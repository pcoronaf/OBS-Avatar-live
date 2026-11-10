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

    def factory(candidate, models, threads, detect_every=6, detector_size=640, options=None):
        rec = sp.Recognizer(sp.make_session(models["arcface_recognition"], candidate, threads))
        sw = sp.Swapper(sp.make_session(models["inswapper_compatible_swapper"], candidate, threads),
                        models["inswapper_compatible_swapper"])
        return sp.SwapPipeline(detector, rec, sw, detect_every)

    return factory


def make_fake_dfm(path: Path, res: int = 224, morph: bool = False) -> Path:
    """DeepFaceLive-style DFM stand-in: celeb face = 1 - input, masks = 0.9 (tests only)."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    one = numpy_helper.from_array(np.array(1.0, np.float32), "one")
    m = numpy_helper.from_array(np.full((1, res, res, 1), 0.9, np.float32), "mask_c")
    nodes = [helper.make_node("Sub", ["one", "in_face:0"], ["inv"]),
             helper.make_node("Identity", ["mask_c"], ["out_face_mask:0"]),
             helper.make_node("Identity", ["mask_c"], ["out_celeb_face_mask:0"])]
    inputs = [helper.make_tensor_value_info("in_face:0", TensorProto.FLOAT, [1, res, res, 3])]
    if morph:
        inputs.append(helper.make_tensor_value_info("morph_value:0", TensorProto.FLOAT, [1]))
        nodes += [helper.make_node("Mul", ["inv", "morph_value:0"], ["out_celeb_face:0"])]
    else:
        nodes += [helper.make_node("Identity", ["inv"], ["out_celeb_face:0"])]
    g = helper.make_graph(nodes, "dfm", inputs, [
        helper.make_tensor_value_info("out_face_mask:0", TensorProto.FLOAT, [1, res, res, 1]),
        helper.make_tensor_value_info("out_celeb_face:0", TensorProto.FLOAT, [1, res, res, 3]),
        helper.make_tensor_value_info("out_celeb_face_mask:0", TensorProto.FLOAT, [1, res, res, 1])], [one, m])
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), str(path))
    return path


def fake_dfm_factory(detector):
    from avatar_engine.engines.dfm_pipeline import DfmModel, DfmPipeline
    from avatar_engine.engines.swap_pipeline import make_session

    def factory(candidate, models, threads, detect_every=6, detector_size=640, options=None):
        o = options or {}
        return DfmPipeline(detector, DfmModel(make_session(models["dfm_face_model"], candidate, threads)),
                           detect_every, o.get("dfm_coverage", 1.0), o.get("dfm_offset_y", 0.0),
                           o.get("dfm_morph", 1.0))

    return factory
