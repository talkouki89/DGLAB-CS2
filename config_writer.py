"""把設定寫回 ``config.toml``。

GUI 的重點是「不用再手改檔案」，但直接把整份設定重新輸出會把註解全部弄丟，
那些註解對使用者其實很有價值。所以這裡採用**就地改值**的做法：

- 只換掉對應 key 的「值」，``#`` 之後的註解原封不動。
- 同一個區塊裡的註解、空行、排列順序都不會被動到。
- key 不存在時補在該區塊的最後面；整個區塊不存在時補一份新的在檔案結尾。
- 寫入前一定會先備份成 ``config.toml.bak``，並且**先驗證過**才寫。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

import tomllib

from config import AppConfig, ConfigError, parse_config

#: 區塊標頭，例如 ``[gsi]``、``[dglab.wave]``
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
#: ``key = value`` 的開頭
_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=")


def format_value(value: Any) -> str:
    """把 Python 值轉成 TOML 字面值。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # TOML 的基本字串與 JSON 字串的轉義規則幾乎一致
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    raise TypeError(f"不支援的設定值型別：{type(value).__name__}")


def config_values(config: AppConfig) -> dict[str, dict[str, Any]]:
    """把 :class:`AppConfig` 攤平成 ``{區塊: {key: 值}}``。"""
    dglab = config.dglab
    policy = dglab.policy
    stack = dglab.stack
    gate = dglab.output
    return {
        "": {"debug": config.debug},
        "gsi": {
            "host": config.gsi.host,
            "port": config.gsi.port,
            "token": config.gsi.token,
        },
        "socket": {
            "uri": config.socket.uri,
            "register_timeout": config.socket.register_timeout,
            "qr_invert": config.socket.qr_invert,
            "qr_png": config.socket.qr_png,
            "qr_open": config.socket.qr_open,
        },
        "dglab": {
            "channels": "".join(sorted(channel.letter for channel in dglab.channels)),
            # policy 才是執行時真正用來夾強度的地方，DglabConfig.max_strength
            # 只是它的鏡像；寫檔案時以 policy 為準。
            "max_strength": policy.max_strength,
            "respect_app_limit": dglab.respect_app_limit,
            "poll_interval": dglab.poll_interval,
            "stop_when_no_data_seconds": dglab.stop_when_no_data_seconds,
            "base_strength": policy.base_strength,
            "strength_per_hp": policy.strength_per_hp,
            "burning_multiplier": policy.burning_multiplier,
            "keep_strength_while_not_injured": policy.keep_strength_while_not_injured,
            "stack_enabled": stack.enabled,
            "stack_death_gain": stack.death_gain,
            "stack_kill_reduction": stack.kill_reduction,
            "stack_min": stack.minimum,
            "stack_max": stack.maximum,
            "output_mode": gate.mode,
            "hit_duration": gate.hit_duration,
            "hit_boost": gate.hit_boost,
            "taser_enabled": dglab.taser.enabled,
            "taser_strength": dglab.taser.strength,
            "taser_duration": dglab.taser.duration,
            "taser_min_damage": dglab.taser.min_damage,
            "death_stop_enabled": dglab.death_stop.enabled,
            "death_stop_delay": dglab.death_stop.delay,
        },
        "dglab.wave": {
            "preset": dglab.wave.preset,
            "custom_samples": [
                [frequency, strength] for frequency, strength in dglab.wave.custom_samples
            ],
        },
        "discord": {
            "enabled": config.discord.enabled,
            "client_id": config.discord.client_id,
            "update_interval": config.discord.update_interval,
            "show_health": config.discord.show_health,
            "show_strength": config.discord.show_strength,
            "show_stack": config.discord.show_stack,
            "large_image": config.discord.large_image,
            "large_text": config.discord.large_text,
            "small_image": config.discord.small_image,
            "small_text": config.discord.small_text,
        },
    }


def _split_comment(line: str) -> tuple[str, str]:
    """把一行切成 ``(程式碼, 註解)``；字串裡的 ``#`` 不算註解。"""
    in_string = False
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string = True
            quote = char
        elif char == "#":
            return line[:index], line[index:]
    return line, ""


def update_toml_text(
    text: str, values: Mapping[str, Mapping[str, Any]]
) -> str:
    """把 ``values`` 套用到 TOML 文字上，盡可能保留原本的排版與註解。

    Args:
        text: 原本的 TOML 內容。
        values: ``{區塊: {key: 值}}``；空字串的區塊代表檔案最前面（沒有標頭）的部分。

    Returns:
        更新後的 TOML 文字。
    """
    pending: dict[str, dict[str, Any]] = {
        section: dict(items) for section, items in values.items()
    }
    seen: set[str] = set()
    lines = text.splitlines()
    out: list[str] = []

    current = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        header = _SECTION_RE.match(line)
        if header:
            # 離開一個區塊前，把它缺少的 key 補在最後
            if current in pending and pending[current]:
                out.extend(_render_lines(current, pending[current]))
                pending[current] = {}
            current = header.group(1).strip()
            seen.add(current)
            pending.setdefault(current, {})
            out.append(line)
            index += 1
            continue

        key_match = _KEY_RE.match(line)
        if (
            key_match
            and current in pending
            and key_match.group(1) in pending[current]
        ):
            key = key_match.group(1)
            value = pending[current].pop(key)
            code, comment = _split_comment(line)
            indent = line[: len(line) - len(line.lstrip())]

            # 多行陣列：整段吃掉再換成一行
            if code.rstrip().endswith("["):
                depth = code.count("[") - code.count("]")
                index += 1
                while index < len(lines) and depth > 0:
                    depth += lines[index].count("[") - lines[index].count("]")
                    index += 1
            else:
                index += 1

            out.append(_render_line(indent, key, value, comment))
            continue

        out.append(line)
        index += 1

    if current in pending and pending[current]:
        out.extend(_render_lines(current, pending[current]))
        pending[current] = {}

    # 整個區塊都不存在時，補在檔案最後
    for section, items in pending.items():
        if not items or section in seen:
            continue
        out.append("")
        if section:
            out.append(f"[{section}]")
        out.extend(_render_lines(section, items))

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def _render_line(indent: str, key: str, value: Any, comment: str = "") -> str:
    line = f"{indent}{key} = {format_value(value)}"
    if comment:
        line += f"  {comment}"
    return line


def _render_lines(section: str, items: Mapping[str, Any]) -> list[str]:
    return [_render_line("", key, value) for key, value in items.items()]


def save_config_values(
    path: str | Path,
    values: Mapping[str, Mapping[str, Any]],
    *,
    backup: bool = True,
) -> Path:
    """就地把值寫進設定檔，並先驗證結果合法。

    Raises:
        ConfigError: 檔案不存在、或更新後的內容不合法（此時不會寫入）。
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"找不到設定檔：{config_path}")

    data = config_path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    original = data.decode("utf-8")

    updated = update_toml_text(original, values)

    # 先確認結果讀得回來而且合法，避免把設定檔寫壞
    try:
        raw = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"更新後的內容不是合法 TOML：{exc}") from exc
    try:
        parse_config(raw)
    except ConfigError as exc:
        raise ConfigError(f"更新後的設定不合法：{exc}") from None

    if backup:
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup_path)

    config_path.write_bytes(updated.encode("utf-8"))
    return config_path


def save_config(config: AppConfig, path: str | Path, *, backup: bool = True) -> Path:
    """把整份 :class:`AppConfig` 寫回設定檔。"""
    return save_config_values(path, config_values(config), backup=backup)
