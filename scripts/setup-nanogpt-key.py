from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "リダイレクトは許可しません。", headers, file_pointer)


def _credentials():
    path = Path(__file__).resolve().parents[1] / "runtime" / "ccproxy-plugins" / "nanogpt" / "credentials.py"
    spec = importlib.util.spec_from_file_location("nanogpt_key_credentials", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("キー暗号化モジュールがありません。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_subscription(api_key: str) -> None:
    request = Request(
        "https://nano-gpt.com/api/subscription/v1/usage",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with build_opener(_NoRedirect()).open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError("サブスク状態を確認できません。キーは保存しません。")
        payload = json.load(response)
    routing = payload.get("routing", {}) if isinstance(payload, dict) else {}
    if not isinstance(routing, dict) or routing.get("paidSpendPolicyAllowsBalance") is not False:
        raise ValueError("NanoGPTのAPIキー設定をSubscription onlyにしてから登録してください。")
    if routing.get("recommendedMode") != "subscription" or routing.get("subscriptionQuotaAvailable") is not True:
        raise ValueError("有効なサブスク利用枠を確認できません。契約とキー設定を確認してください。")


def _save_key(api_key: str) -> Path:
    target = Path.home() / ".claude-codex" / "keys" / "nanogpt.dpapi"
    protected = _credentials().protect_key(api_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    import csv
    account_sid = next(csv.reader([identity.stdout.strip()]))[1]
    if not re.fullmatch(r"S-1-[0-9-]+", account_sid):
        raise ValueError("Windowsユーザーの識別子を確認できません。")
    staging = target.with_name("nanogpt.dpapi.pending-" + uuid4().hex)
    with staging.open("xb") as destination:
        destination.write(protected)
    subprocess.run(
        ["icacls.exe", str(staging), "/inheritance:r", "/grant:r", f"*{account_sid}:F"],
        check=True, capture_output=True,
    )
    if _credentials().read_key(staging) != api_key:
        raise ValueError("保存前の復号検証に失敗しました。旧キーは変更しません。")
    if target.exists():
        backup = target.with_name("nanogpt.dpapi.bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        with backup.open("xb") as destination:
            destination.write(target.read_bytes())
    os.replace(staging, target)
    return target


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Windows専用です。")
    import tkinter as tk
    from tkinter import messagebox

    window = tk.Tk()
    window.title("NanoGPT APIキー登録")
    window.geometry("610x280")
    window.resizable(False, False)
    tk.Label(
        window, text="NanoGPTのAPI画面で作成した専用キーを入力してください。\nBilling modeはSubscription onlyにしてください。\nキーはこのPCのユーザー専用に暗号化し、会話・リポジトリへ保存しません。",
        justify="left", padx=20, pady=20,
    ).pack(anchor="w")
    entry = tk.Entry(window, show="*", width=72)
    entry.pack(padx=20, pady=5)
    status = tk.StringVar(value="キーをチャットへ貼り付ける必要はありません。")
    tk.Label(window, textvariable=status, wraplength=560, justify="left").pack(padx=20, pady=10)

    def save() -> None:
        api_key = entry.get().strip()
        if not api_key or any(character.isspace() for character in api_key):
            messagebox.showerror("入力確認", "有効なAPIキーを入力してください。", parent=window)
            return
        status.set("サブスク枠と従量課金禁止の設定を確認しています…")
        window.update_idletasks()
        try:
            _check_subscription(api_key)
            target = _save_key(api_key)
        except (HTTPError, URLError, TimeoutError):
            status.set("APIキーまたは接続を確認できませんでした。保存していません。")
            return
        except ValueError as error:
            status.set(str(error))
            return
        except (OSError, subprocess.SubprocessError, StopIteration):
            status.set("暗号化保存に失敗しました。保存先とアクセス権を確認してください。")
            return
        entry.delete(0, tk.END)
        messagebox.showinfo("登録完了", f"サブスク限定キーを暗号化保存しました。\n{target}", parent=window)
        window.destroy()

    tk.Button(window, text="サブスク限定を確認して暗号化保存", command=save, padx=10).pack(pady=5)
    entry.focus_set()
    window.mainloop()


if __name__ == "__main__":
    main()
