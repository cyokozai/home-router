"""ブースの NetBox・PostgreSQL・shumoku が使う値を、初回の起動で一度だけ作る。

compose の keygen が、keys ボリューム（/booth-keys）に対して実行する。値はボリュームの中にだけ置き、
表示しない。すでにあるファイルは作り直さない（作り直すと NetBox の API の値と DB の接続が切れるため）。

- nb_vars.sh: NetBox が起動時に読み込む（setup/with-vars.sh）。SECRET_KEY・API_TOKEN_PEPPER_1・DB_PASSWORD
- db_pw: PostgreSQL の POSTGRES_PASSWORD_FILE
- shumoku_admin_pw: shumoku の SHUMOKU_BOOTSTRAP_ADMIN_PASSWORD_FILE
"""

import os
import secrets
import string
import sys
from pathlib import Path

# シェルの '…' にそのまま入れられる文字だけを使う
ALPHABET = string.ascii_letters + string.digits + "-_"


def _random(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    # NetBox（unit）・PostgreSQL（postgres）・shumoku（1000）が別の利用者で読むので、読み取りは全員に許す。
    # ボリュームはホストの root の下にあり、ホストの一般利用者からは見えない
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def ensure_keys(directory: Path) -> list[str]:
    """足りないファイルだけを作り、作ったファイルの名前を返す。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    created = []

    db_pw = directory / "db_pw"
    if not db_pw.exists():
        _write(db_pw, _random(40) + "\n")
        created.append(db_pw.name)
    db_password = db_pw.read_text(encoding="utf-8").strip()

    nb_vars = directory / "nb_vars.sh"
    if not nb_vars.exists():
        lines = [
            f"export SECRET_KEY='{_random(64)}'",
            f"export API_TOKEN_PEPPER_1='{_random(64)}'",
            f"export DB_PASSWORD='{db_password}'",
        ]
        _write(nb_vars, "\n".join(lines) + "\n")
        created.append(nb_vars.name)

    shumoku = directory / "shumoku_admin_pw"
    if not shumoku.exists():
        _write(shumoku, _random(32) + "\n")
        created.append(shumoku.name)

    return created


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "/booth-keys")
    created = ensure_keys(directory)
    print("keygen: 作ったもの: " + (", ".join(created) if created else "なし（すべて作成済み）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
