"""DG-LAB 郊狼 3.0 的 Socket（WebSocket）驅動。

只用 Socket 連線，不需要藍牙：

1. 連上 DG-LAB 的 Socket 中繼服務端（官方為 ``wss://ws.dungeon-lab.cn/``）。
2. 服務端分配一個 ``clientId``，用它產生 QR code 讓 DG-LAB App 掃描。
3. App 掃碼後完成配對，之後就能下強度指令與波形。

典型用法::

    from coyote import (
        Channel, DglabSocketClient, NullTransport, WebsocketsTransport, resolve_preset,
    )

    transport = WebsocketsTransport("wss://ws.dungeon-lab.cn/")
    client = DglabSocketClient(
        transport,
        uri="wss://ws.dungeon-lab.cn/",
        channels=(Channel.A,),
        wave_frames=resolve_preset("steady"),
    )
    await client.start()
    print(client.qr_url)          # 拿去產生 QR code
    while not client.is_paired:   # 等使用者掃碼
        await asyncio.sleep(0.2)
    await client.set_strength(Channel.A, 20)
    client.set_output_active(Channel.A, True)
    ...
    await client.close()
"""

from .socket_client import (
    DEFAULT_REGISTER_TIMEOUT,
    MAX_LEAD_FRAMES,
    MIN_LEAD_FRAMES,
    STREAM_TICK_SECONDS,
    ChannelState,
    DglabSocketClient,
    SocketError,
)
from .socket_protocol import (
    APP_QUEUE_MAX_FRAMES,
    FEEDBACK_BUTTONS,
    MAX_MESSAGE_LENGTH,
    MAX_PULSE_FRAMES_PER_MESSAGE,
    RET_CODE_DESCRIPTION,
    Channel,
    MessageType,
    RetCode,
    ServerFrame,
    StrengthOperation,
    StrengthReport,
    build_message,
    chunk_frames,
    clear_command,
    describe_ret_code,
    parse_feedback,
    parse_server_frame,
    parse_strength_report,
    pulse_command,
    qrcode_url,
    strength_command,
)
from .transport import (
    DEFAULT_URI,
    NullTransport,
    SocketTransport,
    WebsocketsTransport,
)
from .waveform import (
    FRAME_SECONDS,
    PRESETS,
    PRESET_NAMES,
    SAMPLES_PER_FRAME,
    STRENGTH_MAX,
    STRENGTH_MIN,
    WAVE_FREQ_INPUT_MAX,
    WAVE_FREQ_INPUT_MIN,
    WAVE_FREQ_MAX,
    WAVE_FREQ_MIN,
    WAVE_STRENGTH_MAX,
    WAVE_STRENGTH_MIN,
    ChannelWave,
    WaveCycle,
    WaveFrame,
    clamp,
    decode_wave_freq,
    encode_wave_freq,
    frame_from_input,
    from_v2,
    pulse_frequency_hz,
    resolve_preset,
)

__all__ = [
    "APP_QUEUE_MAX_FRAMES",
    "Channel",
    "ChannelState",
    "ChannelWave",
    "DEFAULT_REGISTER_TIMEOUT",
    "DEFAULT_URI",
    "DglabSocketClient",
    "FEEDBACK_BUTTONS",
    "FRAME_SECONDS",
    "MAX_LEAD_FRAMES",
    "MAX_MESSAGE_LENGTH",
    "MAX_PULSE_FRAMES_PER_MESSAGE",
    "MIN_LEAD_FRAMES",
    "MessageType",
    "NullTransport",
    "PRESETS",
    "PRESET_NAMES",
    "RET_CODE_DESCRIPTION",
    "RetCode",
    "SAMPLES_PER_FRAME",
    "STREAM_TICK_SECONDS",
    "STRENGTH_MAX",
    "STRENGTH_MIN",
    "ServerFrame",
    "SocketError",
    "SocketTransport",
    "StrengthOperation",
    "StrengthReport",
    "WAVE_FREQ_INPUT_MAX",
    "WAVE_FREQ_INPUT_MIN",
    "WAVE_FREQ_MAX",
    "WAVE_FREQ_MIN",
    "WAVE_STRENGTH_MAX",
    "WAVE_STRENGTH_MIN",
    "WaveCycle",
    "WaveFrame",
    "WebsocketsTransport",
    "build_message",
    "chunk_frames",
    "clamp",
    "clear_command",
    "decode_wave_freq",
    "describe_ret_code",
    "encode_wave_freq",
    "frame_from_input",
    "from_v2",
    "parse_feedback",
    "parse_server_frame",
    "parse_strength_report",
    "pulse_command",
    "pulse_frequency_hz",
    "qrcode_url",
    "resolve_preset",
    "strength_command",
]

__version__ = "3.1.0"
