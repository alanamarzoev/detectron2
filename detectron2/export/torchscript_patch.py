# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import importlib.util
import os
import sys
import tempfile
from contextlib import contextmanager
import torch

# need an explicit import due to https://github.com/pytorch/pytorch/issues/38964
from detectron2.structures import Boxes, Instances  # noqa F401

_counter = 0


@contextmanager
def patch_instances(fields):
    """
    A contextmanager, under which the Instances class in detectron2 is replaced
    by a statically-typed scriptable class, defined by `fields`.
    See more in `export_torchscript_with_instances`.
    """
    with tempfile.TemporaryDirectory(prefix="detectron2") as dir, tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".py", dir=dir, delete=False
    ) as f:
        try:
            cls_name, s = _gen_module(fields)
            f.write(s)
            f.flush()
            f.close()

            module = _import(f.name)
            new_instances = getattr(module, cls_name)
            _ = torch.jit.script(new_instances)

            # let torchscript think Instances was scripted already
            Instances.__torch_script_class__ = True
            # let torchscript find new_instances when looking for the jit type of Instances
            Instances._jit_override_qualname = torch._jit_internal._qualified_name(new_instances)
            yield new_instances
        finally:
            try:
                del Instances.__torch_script_class__
                del Instances._jit_override_qualname
            except AttributeError:
                pass
            sys.modules.pop(module.__name__)


# TODO: find a more automatic way to enable import of other classes
def _gen_imports():
    imports_str = """
from copy import deepcopy
import torch
from torch import Tensor
import typing
from typing import *

from detectron2.structures import Boxes, Instances

"""
    return imports_str


def _gen_class(fields):
    def indent(level, s):
        return " " * 4 * level + s

    lines = []

    global _counter
    _counter += 1

    cls_name = "Instances_patched{}".format(_counter)

    lines.append(
        f"""
class {cls_name}:
    def __init__(self, image_size: Tuple[int, int]):
        self.image_size = image_size
"""
    )

    for name, type_ in fields.items():
        lines.append(indent(2, f"self._{name} = torch.jit.annotate(Optional[{type_}], None)"))

    for name, type_ in fields.items():
        lines.append(
            f"""
    @property
    def {name}(self) -> {type_}:
        # has to use a local for type refinement
        # https://pytorch.org/docs/stable/jit_language_reference.html#optional-type-refinement
        t = self._{name}
        assert t is not None
        return t

    @{name}.setter
    def {name}(self, value: {type_}) -> None:
        self._{name} = value
"""
        )

    # support function attribute `__len__`
    lines.append(
        """
    def __len__(self) -> int:
"""
    )
    for name, _ in fields.items():
        lines.append(
            f"""
        t = self._{name}
        if t is not None:
            return len(t)
"""
        )
    lines.append(
        """
        raise NotImplementedError("Empty Instances does not support __len__!")
"""
    )

    # support function attribute `has`
    lines.append(
        """
    def has(self, name: str) -> bool:
"""
    )
    for name, _ in fields.items():
        lines.append(
            f"""
        if name == "{name}":
            return self._{name} is not None
"""
        )
    lines.append(
        """
        return False
"""
    )

    # support function attribute `from_instances`
    lines.append(
        f"""
    @torch.jit.unused
    @staticmethod
    def from_instances(instances: Instances) -> "{cls_name}":
        fields = instances.get_fields()
        image_size = instances.image_size
        new_instances = {cls_name}(image_size)
        for name, val in fields.items():
            assert hasattr(new_instances, '_{{}}'.format(name)), \\
                "No attribute named {{}} in {cls_name}".format(name)
            setattr(new_instances, name, deepcopy(val))
        return new_instances
"""
    )
    return cls_name, os.linesep.join(lines)


def _gen_module(fields):
    s = ""
    s += _gen_imports()
    cls_name, cls_def = _gen_class(fields)
    s += cls_def
    return cls_name, s


def _import(path):
    # https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
    spec = importlib.util.spec_from_file_location(
        "{}{}".format(sys.modules[__name__].__name__, _counter), path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module
