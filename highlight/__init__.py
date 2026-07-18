"""Highlight 的公開邊界；外部子系統不要直接依賴 detectors 內部模組。"""


def create_yolo_detector(*args, **kwargs):
    from highlight.detectors.yolo_detector import YOLODetector

    return YOLODetector(*args, **kwargs)


def create_end_graph_detector(*args, **kwargs):
    from highlight.detectors.end_graph_detector import EndGraphDetector

    return EndGraphDetector(*args, **kwargs)
