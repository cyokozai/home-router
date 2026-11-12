# ブースの NetBox に、API の値（v2）を作る。manage.py shell の標準入力に流して使う（setup/connect.sh・setup/run-load.sh。ファイル名に token を含めない）。
#
#   BOOTH_ROLE=viewer        読み取りだけの利用者 booth-viewer の値を作り直す（shumoku が使う）
#   BOOTH_ROLE=writer        書き込みのできる管理者 booth-admin の値を作り直す（seed-empty.sh で load.py が使う）
#   BOOTH_ROLE=revoke-writer booth-admin の値をすべて消す（load.py の後）
#   BOOTH_OUT=<path>         作った値（nbt_<key>.<token>）を書くファイル。コンテナの中の一時ファイルを渡す
#
# 値は表示しない。家の dump を戻したときは、家の値はブースの pepper では通らないので、毎回ここで作り直す。
import os
import sys

from core.models import ObjectType
from users.choices import TokenVersionChoices
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token, User

role = os.environ.get("BOOTH_ROLE", "viewer")
out = os.environ.get("BOOTH_OUT", "")

if role == "viewer":
    user, _ = User.objects.get_or_create(username="booth-viewer")
    user.set_unusable_password()
    user.is_active = True
    user.save()
    perm, _ = ObjectPermission.objects.get_or_create(name="booth-view-all", defaults={"actions": ["view"]})
    perm.actions = ["view"]
    perm.enabled = True
    perm.save()
    perm.object_types.set(ObjectType.objects.public())
    perm.users.add(user)
    write_enabled = False
elif role in ("writer", "revoke-writer"):
    user, created = User.objects.get_or_create(username="booth-admin")
    if created:
        user.set_unusable_password()
    user.is_superuser = True
    user.is_staff = True
    user.is_active = True
    user.save()
    write_enabled = True
else:
    sys.exit(f"nb_access: BOOTH_ROLE が不正: {role}")

Token.objects.filter(user=user).delete()
if role == "revoke-writer":
    print("nb_access: booth-admin の値を消した", file=sys.stderr)
    sys.exit(0)

if not out:
    sys.exit("nb_access: BOOTH_OUT を指定する")
token = Token(user=user, version=TokenVersionChoices.V2, write_enabled=write_enabled, description=f"booth {role}")
token.save()
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"{TOKEN_PREFIX}{token.key}.{token.token}\n")
print(f"nb_access: {user.username} の値を作った（書き込み {'可' if write_enabled else '不可'}）", file=sys.stderr)
