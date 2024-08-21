class ProcessOptions:

    def __init__(self, processordefines:dict, face_distance,  blend_ratio, swap_mode, selected_index, masking_text, imagemask, num_steps, subsample_size, show_face_area, show_mask=False):
        self.processors = processordefines
        self.face_distance_threshold = face_distance
        self.blend_ratio = blend_ratio
        self.swap_mode = swap_mode
        self.selected_index = selected_index
        self.masking_text = masking_text
        self.imagemask = imagemask
        self.num_swap_steps = num_steps
        self.show_face_area_overlay = show_face_area
        self.show_face_masking = show_mask
        self.subsample_size = subsample_size