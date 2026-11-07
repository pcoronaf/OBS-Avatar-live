"""Tiny ONNX stand-ins for the user-supplied recognition/swap models (tests only)."""

from pathlib import Path

import numpy as np


def make_fake_models(d: Path) -> dict:
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    # recognition: 1x3x112x112 -> GAP -> flatten(1x3) -> matmul(3x512)
    w = numpy_helper.from_array(rng.standard_normal((3, 512)).astype(np.float32), "W")
    g = helper.make_graph(
        [helper.make_node("GlobalAveragePool", ["input"], ["gap"]),
         helper.make_node("Flatten", ["gap"], ["flat"]),
         helper.make_node("MatMul", ["flat", "W"], ["embedding"])],
        "rec", [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 112, 112])],
        [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, [1, 512])], [w])
    rec = d / "recognition.onnx"
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), str(rec))
    # swapper: out = target*0.6 + 0.2 + mean(source)*0 ; plus a 512x512 emap initializer (last)
    emap = numpy_helper.from_array(np.eye(512, dtype=np.float32), "emap")
    k = numpy_helper.from_array(np.array(0.6, np.float32), "k")
    b = numpy_helper.from_array(np.array(0.2, np.float32), "b")
    z = numpy_helper.from_array(np.array(0.0, np.float32), "z")
    g = helper.make_graph(
        [helper.make_node("Mul", ["target", "k"], ["t1"]),
         helper.make_node("Add", ["t1", "b"], ["t2"]),
         helper.make_node("ReduceMean", ["source"], ["sm"], keepdims=0),
         helper.make_node("Mul", ["sm", "z"], ["sz"]),
         helper.make_node("Add", ["t2", "sz"], ["output"])],
        "swap",
        [helper.make_tensor_value_info("target", TensorProto.FLOAT, [1, 3, 128, 128]),
         helper.make_tensor_value_info("source", TensorProto.FLOAT, [1, 512])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, 128, 128])], [k, b, z, emap])
    sw = d / "swapper.onnx"
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), str(sw))
    return {"arcface_recognition": rec, "inswapper_compatible_swapper": sw}


def fake_pipeline_factory(detector):
    from avatar_engine.engines import swap_pipeline as sp

    def factory(candidate, models, threads, detect_every=6, detector_size=640):
        rec = sp.Recognizer(sp.make_session(models["arcface_recognition"], candidate, threads))
        sw = sp.Swapper(sp.make_session(models["inswapper_compatible_swapper"], candidate, threads),
                        models["inswapper_compatible_swapper"])
        return sp.SwapPipeline(detector, rec, sw, detect_every)

    return factory
