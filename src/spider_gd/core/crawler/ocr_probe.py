"""验证码依赖能不能真正被当前解释器加载。

打包检查要走和现场一样的导入：``onnxruntime``、它的原生扩展，以及
``ddddocr`` 读模型。只在构建机上 ``LoadLibrary`` 一个 dll 不够。
"""

from __future__ import annotations


def ocr_self_test() -> str:
    """导入现场验证码用到的原生扩展并加载模型。

    Returns:
        str: 成功时以 ``ok`` 开头，并带上扩展文件路径。

    Raises:
        Exception: 导入或加载模型失败。调用方据此让冻结包构建失败。
    """
    import ddddocr
    import onnxruntime
    from onnxruntime.capi import onnxruntime_pybind11_state

    ddddocr.DdddOcr(show_ad=False, beta=True)
    native = getattr(onnxruntime_pybind11_state, "__file__", "") or ""
    return f"ok {onnxruntime.__file__} {native}"
