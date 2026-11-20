import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OrderItemInput(_message.Message):
    __slots__ = ("product_id", "quantity", "unit_price")
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    UNIT_PRICE_FIELD_NUMBER: _ClassVar[int]
    product_id: int
    quantity: int
    unit_price: str
    def __init__(self, product_id: _Optional[int] = ..., quantity: _Optional[int] = ..., unit_price: _Optional[str] = ...) -> None: ...

class OrderItem(_message.Message):
    __slots__ = ("id", "product_id", "quantity", "unit_price")
    ID_FIELD_NUMBER: _ClassVar[int]
    PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    UNIT_PRICE_FIELD_NUMBER: _ClassVar[int]
    id: int
    product_id: int
    quantity: int
    unit_price: str
    def __init__(self, id: _Optional[int] = ..., product_id: _Optional[int] = ..., quantity: _Optional[int] = ..., unit_price: _Optional[str] = ...) -> None: ...

class Order(_message.Message):
    __slots__ = ("id", "total_amount", "status", "created_at", "items", "payment_id", "payment_status")
    ID_FIELD_NUMBER: _ClassVar[int]
    TOTAL_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    PAYMENT_ID_FIELD_NUMBER: _ClassVar[int]
    PAYMENT_STATUS_FIELD_NUMBER: _ClassVar[int]
    id: int
    total_amount: str
    status: str
    created_at: _timestamp_pb2.Timestamp
    items: _containers.RepeatedCompositeFieldContainer[OrderItem]
    payment_id: int
    payment_status: str
    def __init__(self, id: _Optional[int] = ..., total_amount: _Optional[str] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., items: _Optional[_Iterable[_Union[OrderItem, _Mapping]]] = ..., payment_id: _Optional[int] = ..., payment_status: _Optional[str] = ...) -> None: ...

class CreateOrderRequest(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[OrderItemInput]
    def __init__(self, items: _Optional[_Iterable[_Union[OrderItemInput, _Mapping]]] = ...) -> None: ...

class CreateOrderResponse(_message.Message):
    __slots__ = ("order",)
    ORDER_FIELD_NUMBER: _ClassVar[int]
    order: Order
    def __init__(self, order: _Optional[_Union[Order, _Mapping]] = ...) -> None: ...

class GetOrderRequest(_message.Message):
    __slots__ = ("order_id",)
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    order_id: int
    def __init__(self, order_id: _Optional[int] = ...) -> None: ...

class GetOrderResponse(_message.Message):
    __slots__ = ("order",)
    ORDER_FIELD_NUMBER: _ClassVar[int]
    order: Order
    def __init__(self, order: _Optional[_Union[Order, _Mapping]] = ...) -> None: ...
