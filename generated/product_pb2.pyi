import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Product(_message.Message):
    __slots__ = ("id", "name", "description", "price", "created_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: int
    name: str
    description: str
    price: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[int] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., price: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AddProductRequest(_message.Message):
    __slots__ = ("name", "description", "price", "initial_stock")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    INITIAL_STOCK_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    price: str
    initial_stock: int
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., price: _Optional[str] = ..., initial_stock: _Optional[int] = ...) -> None: ...

class AddProductResponse(_message.Message):
    __slots__ = ("product",)
    PRODUCT_FIELD_NUMBER: _ClassVar[int]
    product: Product
    def __init__(self, product: _Optional[_Union[Product, _Mapping]] = ...) -> None: ...

class GetProductRequest(_message.Message):
    __slots__ = ("product_id",)
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    product_id: int
    def __init__(self, product_id: _Optional[int] = ...) -> None: ...

class GetProductResponse(_message.Message):
    __slots__ = ("product",)
    PRODUCT_FIELD_NUMBER: _ClassVar[int]
    product: Product
    def __init__(self, product: _Optional[_Union[Product, _Mapping]] = ...) -> None: ...

class CheckStockRequest(_message.Message):
    __slots__ = ("product_id",)
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    product_id: int
    def __init__(self, product_id: _Optional[int] = ...) -> None: ...

class CheckStockResponse(_message.Message):
    __slots__ = ("product_exists", "available_stock")
    PRODUCT_EXISTS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_STOCK_FIELD_NUMBER: _ClassVar[int]
    product_exists: bool
    available_stock: int
    def __init__(self, product_exists: _Optional[bool] = ..., available_stock: _Optional[int] = ...) -> None: ...

class UpdateStockRequest(_message.Message):
    __slots__ = ("product_id", "quantity_delta")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_DELTA_FIELD_NUMBER: _ClassVar[int]
    product_id: int
    quantity_delta: int
    def __init__(self, product_id: _Optional[int] = ..., quantity_delta: _Optional[int] = ...) -> None: ...

class UpdateStockResponse(_message.Message):
    __slots__ = ("available_stock",)
    AVAILABLE_STOCK_FIELD_NUMBER: _ClassVar[int]
    available_stock: int
    def __init__(self, available_stock: _Optional[int] = ...) -> None: ...
