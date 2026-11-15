import socket


def _blocked(*args, **kwargs):
    raise AssertionError("network call in test")


socket.socket.connect = _blocked
socket.create_connection = _blocked
