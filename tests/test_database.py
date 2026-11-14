from database import Database


def test_panic_has_top_priority(tmp_path):
    db = Database(tmp_path / "s.db")
    db.push_command("RESUME", "t")
    db.push_command("PAUSE", "t")
    db.push_command("PANIC", "t")
    order = [c["command"] for c in db.pending_commands()]
    assert order[0] == "PANIC"
    db.ack_command(db.pending_commands()[0]["id"], "OK")
    assert [c["command"] for c in db.pending_commands()] == ["PAUSE", "RESUME"]


def test_wal_and_concurrent_reader(tmp_path):
    writer = Database(tmp_path / "s.db")
    reader = Database(tmp_path / "s.db", readonly=True)
    mode = writer.query("PRAGMA journal_mode")[0]
    assert list(mode.values())[0] == "wal"
    writer.set_kv("x", {"a": 1})
    assert reader.get_kv("x") == {"a": 1}
