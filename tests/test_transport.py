"""Exercise the production render/flush and real msmtp against loopback SMTP only."""
import socket
import subprocess
import sys
import threading
from email.parser import BytesParser
from email.policy import default

import mddb
import pytest

import mddraft

SUBJECT = "ai@cam supervision pilot — draft for circulation"


def draft(tmp_path):
    db = mddb.MDDB.init(tmp_path / "outbox")
    with db.editor(rationale="transport fixture") as editor:
        card = editor.create(
            title=SUBJECT, summary="fixture",
            yaml={"kind": "draft", "state": "draft", "from": "wh260@cam.ac.uk",
                  "to": ["Toby Lovick <tcl44@cam.ac.uk>"], "subject": SUBJECT},
            body="Harmless local SMTP fixture.\n",
        )
    return db, card.id


@pytest.mark.parametrize("outcome,code", [("accept", 0), ("reject", 69), ("drop", 76), ("auth", 77)])
def test_real_msmtp_boundary(tmp_path, capfd, outcome, code):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    commands, data, errors = [], [], []

    def serve():
        try:
            conn, _ = listener.accept()
            conn.settimeout(5)
            with conn, conn.makefile("rwb", buffering=0) as stream:
                stream.write(b"220 local fixture\r\n")
                in_data = False
                while line := stream.readline():
                    if in_data:
                        if line != b".\r\n":
                            data.append(line)
                            continue
                        in_data = False
                        if outcome == "drop":
                            break
                        stream.write(b"550 rejected\r\n" if outcome == "reject"
                                     else b"250 accepted\r\n")
                    else:
                        commands.append(line)
                        if line.startswith(b"EHLO") and outcome == "auth":
                            stream.write(b"250-local\r\n250 AUTH PLAIN\r\n")
                        elif line.startswith(b"AUTH"):
                            stream.write(b"535 fixture authentication rejected\r\n")
                        elif line.startswith(b"DATA"):
                            stream.write(b"354 proceed\r\n")
                            in_data = True
                        elif line.startswith(b"QUIT"):
                            stream.write(b"221 bye\r\n")
                            break
                        else:
                            stream.write(b"250 local\r\n")
        except OSError as error:
            errors.append(error)
        finally:
            listener.close()

    config = tmp_path / "msmtprc"
    config.write_text(
        "account wh260@cam.ac.uk\nhost 127.0.0.1\n"
        f"port {listener.getsockname()[1]}\ntls off\nauth off\n"
        "from wh260@cam.ac.uk\n"
    )
    if outcome == "auth":
        with config.open("a") as stream:
            stream.write(
                "auth plain\nuser fixture\n"
                "passwordeval printf harmless-password-helper-marker >&2; "
                "printf harmless-fake-password\n"
            )
    thread = threading.Thread(target=serve)
    thread.start()
    db, card_id = draft(tmp_path)
    try:
        if code:
            with pytest.raises(subprocess.CalledProcessError) as caught:
                mddraft.flush(db.root, card_id, db.head(),
                              msmtp=("msmtp", "-C", str(config)))
            assert caught.value.returncode == code
        else:
            mddraft.flush(db.root, card_id, db.head(),
                          msmtp=("msmtp", "-C", str(config)))
    finally:
        thread.join(6)
    assert not thread.is_alive()
    assert not errors
    if outcome != "auth":
        assert b"RCPT TO:<tcl44@cam.ac.uk>\r\n" in commands
        message = BytesParser(policy=default).parsebytes(b"".join(data))
        assert str(message["Subject"]) == SUBJECT
        assert message.get_content() == "Harmless local SMTP fixture.\r\n"
    card = mddb.MDDB(db.root).read(card_id)
    if code:
        assert card.yaml["send_state"] == "ambiguous"
        assert "sent_mid" not in card.yaml
        assert card.yaml["send_error"] == {"returncode": code}
        assert caught.value.stderr is None
        diagnostics = capfd.readouterr().err
        assert "msmtp:" in diagnostics
        if outcome == "auth":
            assert "harmless-password-helper-marker" in diagnostics
            assert "harmless-password-helper-marker" not in str(card.yaml)
            history = subprocess.check_output(["git", "log", "--all", "-p"], cwd=db.root)
            assert b"harmless-password-helper-marker" not in history
        with pytest.raises(mddraft.AmbiguousSend):
            mddraft.flush(db.root, card_id, db.head(), msmtp=("/nonexistent",))
    else:
        assert card.yaml["state"] == "sent"
        assert "send_error" not in card.yaml


def test_exec_failure_is_preserved_without_claiming_smtp_result(tmp_path):
    db, card_id = draft(tmp_path)
    with pytest.raises(FileNotFoundError) as caught:
        mddraft.flush(db.root, card_id, db.head(),
                      msmtp=(str(tmp_path / "missing-msmtp"),))
    card = mddb.MDDB(db.root).read(card_id)
    assert card.yaml["send_error"] == {"errno": caught.value.errno}
    assert card.yaml["send_state"] == "ambiguous"
    assert "sent_mid" not in card.yaml


def test_inherited_stderr_reaches_parent_capture_only(tmp_path):
    db, card_id = draft(tmp_path)
    marker = "harmless-password-helper-marker"
    script = tmp_path / "failing-transport.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stderr.write({marker!r})\n"
        "sys.exit(75)\n"
    )
    # Capture the caller's fd 2, not a pipe made by flush for the transport.
    result = subprocess.run(
        [sys.executable, "-c",
         ("import sys, mddraft; mddraft.flush(sys.argv[1], sys.argv[2], "
          "sys.argv[3], msmtp=(sys.executable, sys.argv[4]))"),
         str(db.root), card_id, db.head(), str(script)],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert marker.encode() in result.stderr
    card = mddb.MDDB(db.root).read(card_id)
    assert card.yaml["send_error"] == {"returncode": 75}
    assert marker not in str(card.yaml)
    history = subprocess.check_output(["git", "log", "--all", "-p"], cwd=db.root)
    assert marker.encode() not in history
