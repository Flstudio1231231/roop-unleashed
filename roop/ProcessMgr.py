import os
import cv2 
import numpy as np
import psutil

from enum import Enum
from roop.ProcessOptions import ProcessOptions

from roop.face_util import get_first_face, get_all_faces, rotate_anticlockwise, rotate_clockwise, clamp_cut_values
from roop.utilities import compute_cosine_distance, get_device, str_to_class
import roop.vr_util as vr

from typing import Any, List, Callable
from roop.typing import Frame, Face
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread, Lock
from queue import Queue
from tqdm import tqdm
from roop.ffmpeg_writer import FFMPEG_VideoWriter
import roop.globals



# Poor man's enum to be able to compare to int
class eNoFaceAction():
    USE_ORIGINAL_FRAME = 0
    RETRY_ROTATED = 1
    SKIP_FRAME = 2
    SKIP_FRAME_IF_DISSIMILAR = 3



def create_queue(temp_frame_paths: List[str]) -> Queue[str]:
    queue: Queue[str] = Queue()
    for frame_path in temp_frame_paths:
        queue.put(frame_path)
    return queue


def pick_queue(queue: Queue[str], queue_per_future: int) -> List[str]:
    queues = []
    for _ in range(queue_per_future):
        if not queue.empty():
            queues.append(queue.get())
    return queues



class ProcessMgr():
    input_face_datas = []
    target_face_datas = []

    imagemask = None

    processors = []
    options : ProcessOptions = None
    
    num_threads = 1
    current_index = 0
    processing_threads = 1
    buffer_wait_time = 0.1

    lock = Lock()

    frames_queue = None
    processed_queue = None

    videowriter= None

    progress_gradio = None
    total_frames = 0

    


    plugins =  { 
    'faceswap'          : 'FaceSwapInsightFace',
    'mask_clip2seg'     : 'Mask_Clip2Seg',
    'mask_xseg'         : 'Mask_XSeg',
    'codeformer'        : 'Enhance_CodeFormer',
    'gfpgan'            : 'Enhance_GFPGAN',
    'dmdnet'            : 'Enhance_DMDNet',
    'gpen'              : 'Enhance_GPEN',
    'restoreformer++'   : 'Enhance_RestoreFormerPPlus',
    'colorizer'         : 'Frame_Colorizer',
    'filter_generic'    : 'Frame_Filter',
    'removebg'          : 'Frame_Masking',
    'upscale'           : 'Frame_Upscale'
    }

    def __init__(self, progress):
        if progress is not None:
            self.progress_gradio = progress

    def reuseOldProcessor(self, name:str):
        for p in self.processors:
            if p.processorname == name:
                return p
            
        return None


    def initialize(self, input_faces, target_faces, options):
        self.input_face_datas = input_faces
        self.target_face_datas = target_faces
        self.options = options
        devicename = get_device()

        roop.globals.g_desired_face_analysis=["landmark_3d_68", "landmark_2d_106","detection","recognition"]
        if options.swap_mode == "all_female" or options.swap_mode == "all_male":
            roop.globals.g_desired_face_analysis.append("genderage")

        for p in self.processors:
            newp = next((x for x in options.processors.keys() if x == p.processorname), None)
            if newp is None:
                p.Release()
                del p

        newprocessors = []
        for key, extoption in options.processors.items():
            p = self.reuseOldProcessor(key)
            if p is None:
                classname = self.plugins[key]
                module = 'roop.processors.' + classname
                p = str_to_class(module, classname)
            if p is not None:
                extoption.update({"devicename": devicename})
                p.Initialize(extoption)
                newprocessors.append(p)
            else:
                print(f"Not using {module}")
        self.processors = newprocessors



        if isinstance(self.options.imagemask, dict) and self.options.imagemask.get("layers") and len(self.options.imagemask["layers"]) > 0:
            self.options.imagemask  = self.options.imagemask.get("layers")[0]
            # Get rid of alpha
            self.options.imagemask = cv2.cvtColor(self.options.imagemask, cv2.COLOR_RGBA2GRAY)
            if np.any(self.options.imagemask):
                mo = self.input_face_datas[0].faces[0].mask_offsets
                self.options.imagemask = self.blur_area(self.options.imagemask, mo[4], mo[5])
                self.options.imagemask = self.options.imagemask.astype(np.float32) / 255
                self.options.imagemask = cv2.cvtColor(self.options.imagemask, cv2.COLOR_GRAY2RGB)
            else:
                self.options.imagemask = None

        self.options.frame_processing = False
        for p in self.processors:
            if p.type.startswith("frame_"):
                self.options.frame_processing = True

            
 



    def run_batch(self, source_files, target_files, threads:int = 1):
        progress_bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]'
        self.total_frames = len(source_files)
        self.num_threads = threads
        with tqdm(total=self.total_frames, desc='Processing', unit='frame', dynamic_ncols=True, bar_format=progress_bar_format) as progress:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = []
                queue = create_queue(source_files)
                queue_per_future = max(len(source_files) // threads, 1)
                while not queue.empty():
                    future = executor.submit(self.process_frames, source_files, target_files, pick_queue(queue, queue_per_future), lambda: self.update_progress(progress))
                    futures.append(future)
                for future in as_completed(futures):
                    future.result()


    def process_frames(self, source_files: List[str], target_files: List[str], current_files, update: Callable[[], None]) -> None:
        for f in current_files:
            if not roop.globals.processing:
                return
            
            # Decode the byte array into an OpenCV image
            temp_frame = cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR)
            if temp_frame is not None:
                if self.options.frame_processing:
                    for p in self.processors:
                        frame = p.Run(temp_frame)
                    resimg = frame
                else:
                    resimg = self.process_frame(temp_frame)
                if resimg is not None:
                    i = source_files.index(f)
                    cv2.imwrite(target_files[i], resimg)
            if update:
                update()



    def read_frames_thread(self, cap, frame_start, frame_end, num_threads):
        num_frame = 0
        total_num = frame_end - frame_start
        if frame_start > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES,frame_start)

        while True and roop.globals.processing:
            ret, frame = cap.read()
            if not ret:
                break
                
            self.frames_queue[num_frame % num_threads].put(frame, block=True)
            num_frame += 1
            if num_frame == total_num:
                break

        for i in range(num_threads):
            self.frames_queue[i].put(None)



    def process_videoframes(self, threadindex, progress) -> None:
        while True:
            frame = self.frames_queue[threadindex].get()
            if frame is None:
                self.processing_threads -= 1
                self.processed_queue[threadindex].put((False, None))
                return
            else:
                if self.options.frame_processing:
                    for p in self.processors:
                        frame = p.Run(frame)
                    resimg = frame
                else:                            
                    resimg = self.process_frame(frame)
                self.processed_queue[threadindex].put((True, resimg))
                del frame
                progress()


    def write_frames_thread(self):
        nextindex = 0
        num_producers = self.num_threads
        
        while True:
            process, frame = self.processed_queue[nextindex % self.num_threads].get()
            nextindex += 1
            if frame is not None:
                self.videowriter.write_frame(frame)
                del frame
            elif process == False:
                num_producers -= 1
                if num_producers < 1:
                    return
            


    def run_batch_inmem(self, source_video, target_video, frame_start, frame_end, fps, threads:int = 1, skip_audio=False):
        cap = cv2.VideoCapture(source_video)
        # frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_count = (frame_end - frame_start) + 1
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        processed_resolution = None
        for p in self.processors:
            if hasattr(p, 'getProcessedResolution'):
                processed_resolution = p.getProcessedResolution(width, height)
                print(f"Processed resolution: {processed_resolution}")
        if processed_resolution is not None:
            width = processed_resolution[0]
            height = processed_resolution[1]


        self.total_frames = frame_count
        self.num_threads = threads

        self.processing_threads = self.num_threads
        self.frames_queue = []
        self.processed_queue = []
        for _ in range(threads):
            self.frames_queue.append(Queue(1))
            self.processed_queue.append(Queue(1))

        self.videowriter =  FFMPEG_VideoWriter(target_video, (width, height), fps, codec=roop.globals.video_encoder, crf=roop.globals.video_quality, audiofile=None)

        readthread = Thread(target=self.read_frames_thread, args=(cap, frame_start, frame_end, threads))
        readthread.start()

        writethread = Thread(target=self.write_frames_thread)
        writethread.start()

        progress_bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]'
        with tqdm(total=self.total_frames, desc='Processing', unit='frames', dynamic_ncols=True, bar_format=progress_bar_format) as progress:
            with ThreadPoolExecutor(thread_name_prefix='swap_proc', max_workers=self.num_threads) as executor:
                futures = []
                
                for threadindex in range(threads):
                    future = executor.submit(self.process_videoframes, threadindex, lambda: self.update_progress(progress))
                    futures.append(future)
                
                for future in as_completed(futures):
                    future.result()
        # wait for the task to complete
        readthread.join()
        writethread.join()
        cap.release()
        self.videowriter.close()
        self.frames_queue.clear()
        self.processed_queue.clear()




    def update_progress(self, progress: Any = None) -> None:
        process = psutil.Process(os.getpid())
        memory_usage = process.memory_info().rss / 1024 / 1024 / 1024
        progress.set_postfix({
            'memory_usage': '{:.2f}'.format(memory_usage).zfill(5) + 'GB',
            'execution_threads': self.num_threads
        })
        progress.update(1)
        if self.progress_gradio is not None:
            self.progress_gradio((progress.n, self.total_frames), desc='Processing', total=self.total_frames, unit='frames')



    def process_frame(self, frame:Frame):
        if len(self.input_face_datas) < 1 and not self.options.show_face_masking:
            return frame
        temp_frame = frame.copy()
        num_swapped, temp_frame = self.swap_faces(frame, temp_frame)
        if num_swapped > 0:
            if roop.globals.no_face_action == eNoFaceAction.SKIP_FRAME_IF_DISSIMILAR:
                if len(self.input_face_datas) > num_swapped:
                    return None
            return temp_frame
        if roop.globals.no_face_action == eNoFaceAction.USE_ORIGINAL_FRAME:
            return frame
        if roop.globals.no_face_action == eNoFaceAction.SKIP_FRAME:
            #This only works with in-mem processing, as it simply skips the frame.
            #For 'extract frames' it simply leaves the unprocessed frame unprocessed and it gets used in the final output by ffmpeg.
            #If we could delete that frame here, that'd work but that might cause ffmpeg to fail unless the frames are renamed, and I don't think we have the info on what frame it actually is?????
            #alternatively, it could mark all the necessary frames for deletion, delete them at the end, then rename the remaining frames that might work?
            return None
        else:
            return self.retry_rotated(frame)

    def retry_rotated(self, frame):
        copyframe = frame.copy()
        copyframe = rotate_clockwise(copyframe)
        temp_frame = copyframe.copy()
        num_swapped, temp_frame = self.swap_faces(copyframe, temp_frame)
        if num_swapped > 0:
            return rotate_anticlockwise(temp_frame)
        
        copyframe = frame.copy()
        copyframe = rotate_anticlockwise(copyframe)
        temp_frame = copyframe.copy()
        num_swapped, temp_frame = self.swap_faces(copyframe, temp_frame)
        if num_swapped > 0:
            return rotate_clockwise(temp_frame)
        del copyframe
        return frame
        


    def swap_faces(self, frame, temp_frame):
        num_faces_found = 0

        if self.options.swap_mode == "first":
            face = get_first_face(frame)

            if face is None:
                return num_faces_found, frame
            
            num_faces_found += 1
            temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
        else:
            faces = get_all_faces(frame)
            if faces is None:
                return num_faces_found, frame
            
            if self.options.swap_mode == "all":
                for face in faces:
                    num_faces_found += 1
                    temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
                    del face
            
            elif self.options.swap_mode == "selected":
                num_targetfaces = len(self.target_face_datas) 
                use_index = num_targetfaces == 1
                for i,tf in enumerate(self.target_face_datas):
                    for face in faces:
                        if compute_cosine_distance(tf.embedding, face.embedding) <= self.options.face_distance_threshold:
                            if i < len(self.input_face_datas):
                                if use_index:
                                    temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
                                else:
                                    temp_frame = self.process_face(i, face, temp_frame)
                                num_faces_found += 1
                            del face
                            if not roop.globals.vr_mode and num_faces_found == num_targetfaces:
                                break
            elif self.options.swap_mode == "all_female" or self.options.swap_mode == "all_male":
                gender = 'F' if self.options.swap_mode == "all_female" else 'M'
                for face in faces:
                    if face.sex == gender:
                        num_faces_found += 1
                        temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
                    del face

        if roop.globals.vr_mode and num_faces_found % 2 > 0:
            # stereo image, there has to be an even number of faces
            num_faces_found = 0
            return num_faces_found, frame
        if num_faces_found == 0:
            return num_faces_found, frame

        #maskprocessor = next((x for x in self.processors if x.type == 'mask'), None)

        if self.options.imagemask is not None and self.options.imagemask.shape == frame.shape:
            temp_frame = self.simple_blend_with_mask(temp_frame, frame, self.options.imagemask)
        return num_faces_found, temp_frame


    def rotation_action(self, original_face:Face, frame:Frame):
        (height, width) = frame.shape[:2]

        bounding_box_width = original_face.bbox[2] - original_face.bbox[0]
        bounding_box_height = original_face.bbox[3] - original_face.bbox[1]
        horizontal_face = bounding_box_width > bounding_box_height

        center_x = width // 2.0
        start_x = original_face.bbox[0]
        end_x = original_face.bbox[2]
        bbox_center_x = start_x + (bounding_box_width // 2.0)

        # need to leverage the array of landmarks as decribed here:
        # https://github.com/deepinsight/insightface/tree/master/alignment/coordinate_reg
        # basically, we should be able to check for the relative position of eyes and nose
        # then use that to determine which way the face is actually facing when in a horizontal position
        # and use that to determine the correct rotation_action

        forehead_x = original_face.landmark_2d_106[72][0]
        chin_x = original_face.landmark_2d_106[0][0]

        if horizontal_face:
            if chin_x < forehead_x:
                # this is someone lying down with their face like this (:
                return "rotate_anticlockwise"
            elif forehead_x < chin_x:
                # this is someone lying down with their face like this :)
                return "rotate_clockwise"
            if bbox_center_x >= center_x:
                # this is someone lying down with their face in the right hand side of the frame
                return "rotate_anticlockwise"
            if bbox_center_x < center_x:
                # this is someone lying down with their face in the left hand side of the frame
                return "rotate_clockwise"

        return None


    def auto_rotate_frame(self, original_face, frame:Frame):
        target_face = original_face
        original_frame = frame

        rotation_action = self.rotation_action(original_face, frame)

        if rotation_action == "rotate_anticlockwise":
            #face is horizontal, rotating frame anti-clockwise and getting face bounding box from rotated frame
            frame = rotate_anticlockwise(frame)
        elif rotation_action == "rotate_clockwise":
            #face is horizontal, rotating frame clockwise and getting face bounding box from rotated frame
            frame = rotate_clockwise(frame)

        return target_face, frame, rotation_action
    

    def auto_unrotate_frame(self, frame:Frame, rotation_action):
        if rotation_action == "rotate_anticlockwise":
            return rotate_clockwise(frame)
        elif rotation_action == "rotate_clockwise":
            return rotate_anticlockwise(frame)
        
        return frame



    def process_face(self,face_index, target_face:Face, frame:Frame):
        from roop.face_util import align_crop

        enhanced_frame = None
        if(len(self.input_face_datas) > 0):
            inputface = self.input_face_datas[face_index].faces[0]
        else:
            inputface = None

        rotation_action = None
        if roop.globals.autorotate_faces:
            # check for sideways rotation of face
            rotation_action = self.rotation_action(target_face, frame)
            if rotation_action is not None:
                (startX, startY, endX, endY) = target_face["bbox"].astype("int")
                width = endX - startX
                height = endY - startY
                offs = int(max(width,height) * 0.25)
                rotcutframe,startX, startY, endX, endY = self.cutout(frame, startX - offs, startY - offs, endX + offs, endY + offs)
                if rotation_action == "rotate_anticlockwise":
                    rotcutframe = rotate_anticlockwise(rotcutframe)
                elif rotation_action == "rotate_clockwise":
                    rotcutframe = rotate_clockwise(rotcutframe)
                # rotate image and re-detect face to correct wonky landmarks
                rotface = get_first_face(rotcutframe)
                if rotface is None:
                    rotation_action = None
                else:
                    saved_frame = frame.copy()
                    frame = rotcutframe
                    target_face = rotface



        # if roop.globals.vr_mode:
            # bbox = target_face.bbox
            # [orig_width, orig_height, _] = frame.shape

            # # Convert bounding box to ints
            # x1, y1, x2, y2 = map(int, bbox)

            # # Determine the center of the bounding box
            # x_center = (x1 + x2) / 2
            # y_center = (y1 + y2) / 2

            # # Normalize coordinates to range [-1, 1]
            # x_center_normalized = x_center / (orig_width / 2) - 1
            # y_center_normalized = y_center / (orig_width / 2) - 1

            # # Convert normalized coordinates to spherical (theta, phi)
            # theta = x_center_normalized * 180  # Theta ranges from -180 to 180 degrees
            # phi = -y_center_normalized * 90  # Phi ranges from -90 to 90 degrees

            # img = vr.GetPerspective(frame, 90, theta, phi, 1280, 1280)  # Generate perspective image


        """ Code ported/adapted from Facefusion which borrowed the idea from Rope:
            Kind of subsampling the cutout and aligned face image and faceswapping slices of it up to
            the desired output resolution. This works around the current resolution limitations without using enhancers.
        """
        model_output_size = 128
        subsample_size = self.options.subsample_size
        subsample_total = subsample_size // model_output_size
        aligned_img, M = align_crop(frame, target_face.kps, subsample_size)

        fake_frame = aligned_img
        target_face.matrix = M

        for p in self.processors:
            if p.type == 'swap':
                swap_result_frames = []
                subsample_frames = self.implode_pixel_boost(aligned_img, model_output_size, subsample_total)
                for sliced_frame in subsample_frames:
                    for _ in range(0,self.options.num_swap_steps):
                        sliced_frame = self.prepare_crop_frame(sliced_frame)
                        sliced_frame = p.Run(inputface, target_face, sliced_frame)
                        sliced_frame = self.normalize_swap_frame(sliced_frame)
                    swap_result_frames.append(sliced_frame)
                fake_frame = self.explode_pixel_boost(swap_result_frames, model_output_size, subsample_total, subsample_size)
                fake_frame = fake_frame.astype(np.uint8)
                scale_factor = 0.0
            elif p.type == 'mask':
                fake_frame = self.process_mask(p, aligned_img, fake_frame)
            else:
                enhanced_frame, scale_factor = p.Run(self.input_face_datas[face_index], target_face, fake_frame)

        upscale = 512
        orig_width = fake_frame.shape[1]
        if orig_width != upscale:
            fake_frame = cv2.resize(fake_frame, (upscale, upscale), cv2.INTER_CUBIC)
        mask_offsets = (0,0,0,0,1,20) if inputface is None else inputface.mask_offsets

        
        if enhanced_frame is None:
            scale_factor = int(upscale / orig_width)
            result = self.paste_upscale(fake_frame, fake_frame, target_face.matrix, frame, scale_factor, mask_offsets)
        else:
            result = self.paste_upscale(fake_frame, enhanced_frame, target_face.matrix, frame, scale_factor, mask_offsets)

        if rotation_action is not None:
            fake_frame = self.auto_unrotate_frame(result, rotation_action)
            return self.paste_simple(fake_frame, saved_frame, startX, startY)
        
        return result

        


    def cutout(self, frame:Frame, start_x, start_y, end_x, end_y):
        if start_x < 0:
            start_x = 0
        if start_y < 0:
            start_y = 0
        if end_x > frame.shape[1]:
            end_x = frame.shape[1]
        if end_y > frame.shape[0]:
            end_y = frame.shape[0]
        return frame[start_y:end_y, start_x:end_x], start_x, start_y, end_x, end_y

    def paste_simple(self, src:Frame, dest:Frame, start_x, start_y):
        end_x = start_x + src.shape[1]
        end_y = start_y + src.shape[0]

        start_x, end_x, start_y, end_y = clamp_cut_values(start_x, end_x, start_y, end_y, dest)
        dest[start_y:end_y, start_x:end_x] = src
        return dest
        
    def simple_blend_with_mask(self, image1, image2, mask):
        # Blend the images
        blended_image = image1.astype(np.float32) * (1.0 - mask) + image2.astype(np.float32) * mask
        return blended_image.astype(np.uint8)


    def paste_upscale(self, fake_face, upsk_face, M, target_img, scale_factor, mask_offsets):
        M_scale = M * scale_factor
        IM = cv2.invertAffineTransform(M_scale)

        face_matte = np.full((target_img.shape[0],target_img.shape[1]), 255, dtype=np.uint8)
        # Generate white square sized as a upsk_face
        img_matte = np.zeros((upsk_face.shape[0],upsk_face.shape[1]), dtype=np.uint8)

        w = img_matte.shape[1]
        h = img_matte.shape[0]

        top = int(mask_offsets[0] * h)
        bottom = int(h - (mask_offsets[1] * h))
        left = int(mask_offsets[2] * w)
        right = int(w - (mask_offsets[3] * w))
        img_matte[top:bottom,left:right] = 255

        # Transform white square back to target_img
        img_matte = cv2.warpAffine(img_matte, IM, (target_img.shape[1], target_img.shape[0]), flags=cv2.INTER_NEAREST, borderValue=0.0) 
        ##Blacken the edges of face_matte by 1 pixels (so the mask in not expanded on the image edges)
        img_matte[:1,:] = img_matte[-1:,:] = img_matte[:,:1] = img_matte[:,-1:] = 0

        img_matte = self.blur_area(img_matte, mask_offsets[4], mask_offsets[5])
        #Normalize images to float values and reshape
        img_matte = img_matte.astype(np.float32)/255
        face_matte = face_matte.astype(np.float32)/255
        img_matte = np.minimum(face_matte, img_matte)
        if self.options.show_face_area_overlay:
            # Additional steps for green overlay
            green_overlay = np.zeros_like(target_img)
            green_color = [0, 255, 0]  # RGB for green
            for i in range(3):  # Apply green color where img_matte is not zero
                green_overlay[:, :, i] = np.where(img_matte > 0, green_color[i], 0)        ##Transform upcaled face back to target_img
        img_matte = np.reshape(img_matte, [img_matte.shape[0],img_matte.shape[1],1]) 
        paste_face = cv2.warpAffine(upsk_face, IM, (target_img.shape[1], target_img.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        if upsk_face is not fake_face:
            fake_face = cv2.warpAffine(fake_face, IM, (target_img.shape[1], target_img.shape[0]), borderMode=cv2.BORDER_REPLICATE)
            paste_face = cv2.addWeighted(paste_face, self.options.blend_ratio, fake_face, 1.0 - self.options.blend_ratio, 0)

        # Re-assemble image
        paste_face = img_matte * paste_face
        paste_face = paste_face + (1-img_matte) * target_img.astype(np.float32)
        if self.options.show_face_area_overlay:
            # Overlay the green overlay on the final image
            paste_face = cv2.addWeighted(paste_face.astype(np.uint8), 1 - 0.5, green_overlay, 0.5, 0)
        return paste_face.astype(np.uint8)


    def blur_area(self, img_matte, num_erosion_iterations, blur_amount):
        # Detect the affine transformed white area
        mask_h_inds, mask_w_inds = np.where(img_matte==255) 
        # Calculate the size (and diagonal size) of transformed white area width and height boundaries
        mask_h = np.max(mask_h_inds) - np.min(mask_h_inds) 
        mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
        mask_size = int(np.sqrt(mask_h*mask_w))
        # Calculate the kernel size for eroding img_matte by kernel (insightface empirical guess for best size was max(mask_size//10,10))
        # k = max(mask_size//12, 8)
        k = max(mask_size//(blur_amount // 2) , blur_amount // 2)
        kernel = np.ones((k,k),np.uint8)
        img_matte = cv2.erode(img_matte,kernel,iterations = num_erosion_iterations)
        #Calculate the kernel size for blurring img_matte by blur_size (insightface empirical guess for best size was max(mask_size//20, 5))
        # k = max(mask_size//24, 4) 
        k = max(mask_size//blur_amount, blur_amount//5) 
        kernel_size = (k, k)
        blur_size = tuple(2*i+1 for i in kernel_size)
        return cv2.GaussianBlur(img_matte, blur_size, 0)


    def prepare_crop_frame(self, swap_frame):
        model_type = 'inswapper'
        model_mean = [0.0, 0.0, 0.0]
        model_standard_deviation = [1.0, 1.0, 1.0]

        if model_type == 'ghost':
            swap_frame = swap_frame[:, :, ::-1] / 127.5 - 1
        else:
            swap_frame = swap_frame[:, :, ::-1] / 255.0
        swap_frame = (swap_frame - model_mean) / model_standard_deviation
        swap_frame = swap_frame.transpose(2, 0, 1)
        swap_frame = np.expand_dims(swap_frame, axis = 0).astype(np.float32)
        return swap_frame


    def normalize_swap_frame(self, swap_frame):
        model_type = 'inswapper'
        swap_frame = swap_frame.transpose(1, 2, 0)

        if model_type == 'ghost':
            swap_frame = (swap_frame * 127.5 + 127.5).round()
        else:
            swap_frame = (swap_frame * 255.0).round()
        swap_frame = swap_frame[:, :, ::-1]
        return swap_frame

    def implode_pixel_boost(self, aligned_face_frame, model_size, pixel_boost_total : int):
        subsample_frame = aligned_face_frame.reshape(model_size, pixel_boost_total, model_size, pixel_boost_total, 3)
        subsample_frame = subsample_frame.transpose(1, 3, 0, 2, 4).reshape(pixel_boost_total ** 2, model_size, model_size, 3)
        return subsample_frame


    def explode_pixel_boost(self, subsample_frame, model_size, pixel_boost_total, pixel_boost_size):
        final_frame = np.stack(subsample_frame, axis = 0).reshape(pixel_boost_total, pixel_boost_total, model_size, model_size, 3)
        final_frame = final_frame.transpose(2, 0, 3, 1, 4).reshape(pixel_boost_size, pixel_boost_size, 3)
        return final_frame

    def process_mask(self, processor, frame:Frame, target:Frame):
        img_mask = processor.Run(frame, self.options.masking_text)
        img_mask = cv2.resize(img_mask, (target.shape[1], target.shape[0]))
        img_mask = np.reshape(img_mask, [img_mask.shape[0],img_mask.shape[1],1])

        if self.options.show_face_masking:
            result = (1 - img_mask) * frame.astype(np.float32)
            return np.uint8(result)


        target = target.astype(np.float32)
        result = (1-img_mask) * target
        result += img_mask * frame.astype(np.float32)
        return np.uint8(result)

            


    def unload_models():
        pass


    def release_resources(self):
        for p in self.processors:
            p.Release()
        self.processors.clear()

