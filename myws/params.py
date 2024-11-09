import numpy as np
input_size = 640
yaml_file = 'D:/VS_ws/python/mocap/utils/args.yaml'
weights_file = 'D:/VS_ws/python/mocap/weights/best.pt'
onnx_file = 'D:/VS_ws/python/mocap/weights/best.onnx'
trt_file = 'path/to/trt_engine.engine'  # .trt or .engine file path
trt_max_batch_size = 1              # max batch size for TRT engine, this is set during engine creation
trt_input_dtype = np.float16        # fp16 or fp32
video_path = 'D:/VS_ws/python/mocap/myws/data/speedup.mp4'


class MODEL_TYPE():
    ONNX = "onnx"
    PT = "pt"
    TRT = "trt"
model_type = MODEL_TYPE.ONNX

VISUALIZE_DRAW = True       # if True, draw detections on frame
VISUALIZE_PLOT = True    # if True, plot the skeleton using matplotlib
USE_CAMERA = False   # if True, use camera to capture video, otherwise use video file
TRANS_H36M = True    # if True, use H36M skeleton, otherwise use COCO skeleton




