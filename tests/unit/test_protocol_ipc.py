import threading

import pytest

from avatar_engine import ipc, protocol


def test_encode_decode():
    data = protocol.encode(protocol.request(3, "ping"))
    n = protocol.decode_length(data[:4])
    msg = protocol.decode_body(data[4:4 + n])
    assert msg == {"v": 1, "id": 3, "type": "ping", "payload": {}}


@pytest.mark.parametrize("body,code", [(b"[]", "bad_envelope"), (b'{"v":2,"type":"x"}', "bad_version"),
                                       (b"{nope", "bad_json")])
def test_bad_messages(body, code):
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.decode_body(body)
    assert e.value.code == code


def test_too_large():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_length((2 << 20).to_bytes(4, "little"))


def test_socket_roundtrip(session_name):
    srv = ipc.create_server(session_name)
    got = {}

    def serve():
        c = None
        while c is None:
            c = srv.accept()
        got["msg"] = c.recv()
        c.send(protocol.reply_ok(got["msg"]["id"], {"x": "ñ"}))
        got["peer"] = c.peer_pid
        c.close()

    t = threading.Thread(target=serve)
    t.start()
    cli = ipc.connect(session_name)
    cli.send(protocol.request(1, "ping"))
    r = cli.recv()
    t.join(5)
    cli.close()
    srv.close()
    assert r["ok"] and r["payload"]["x"] == "ñ"
    assert got["msg"]["type"] == "ping"
