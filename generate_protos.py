import re
from pathlib import Path

import grpc_tools
from grpc_tools import protoc


ROOT = Path(__file__).resolve().parent
PROTO_DIR = ROOT / "protos"
GENERATED_DIR = ROOT / "generated"
WELL_KNOWN_PROTO_DIR = Path(grpc_tools.__file__).resolve().parent / "_proto"


def main() -> None:
    GENERATED_DIR.mkdir(exist_ok=True)
    (GENERATED_DIR / "__init__.py").touch(exist_ok=True)

    proto_files = sorted(PROTO_DIR.glob("*.proto"))
    if not proto_files:
        raise FileNotFoundError(f"No .proto files found in {PROTO_DIR}")

    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"-I{WELL_KNOWN_PROTO_DIR}",
            f"--python_out={GENERATED_DIR}",
            f"--pyi_out={GENERATED_DIR}",
            f"--grpc_python_out={GENERATED_DIR}",
            *(str(proto_file) for proto_file in proto_files),
        ]
    )
    if result != 0:
        raise SystemExit(result)

    grpc_import = re.compile(
        r"(?m)^import ([A-Za-z_][A-Za-z0-9_]*_pb2) as ([A-Za-z_][A-Za-z0-9_]*)$"
    )
    for grpc_file in GENERATED_DIR.glob("*_pb2_grpc.py"):
        source = grpc_file.read_text(encoding="utf-8")
        grpc_file.write_text(
            grpc_import.sub(r"from . import \1 as \2", source),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
