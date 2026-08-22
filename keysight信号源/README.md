# Keysight N5171B Python 控制器

这是一个纯 Python、无第三方依赖的 N5171B 控制器。计算机通过网线连接信号源，使用 `TCP 5025` 端口发送 SCPI 指令。

## 获取仪器 IP 地址

在 N5171B 前面板依次进入：

```text
Utility > I/O Config > LAN Setup > Manual Config Settings > IP Address
```

如果 `Config Type` 是 `Auto (DHCP/Auto-IP)`，先连接网线并等待仪器获得地址，再进入上述页面查看。如果没有 DHCP，也可以选择 `Manual`，手动给仪器和电脑设置同一网段且不重复的地址，例如：

```text
仪器: 192.168.10.2
电脑: 192.168.10.1
子网掩码: 255.255.255.0
```

本项目通过 TCP 5025 直接发送 SCPI，因此还要确认仪器的 Socket 服务已启用：

```text
Utility > I/O Config > LAN Services Setup > SCPI Services
> Sockets SCPI: On > Proceed With Reconfiguration
```

完成后可在电脑上执行 `ping 192.168.10.2` 检查网络是否连通，再用 Python 查询仪器型号：

```python
from n5171b import N5171B

source = N5171B("192.168.10.2")
print(source.connect())  # 应返回包含 Keysight,N5171B 的识别信息
source.close()
```

## 模块结构

```text
n5171b/
|-- connection.py   # 网线连接、SCPI 指令发送和查询
|-- output.py       # 固定频率和 RF 开关
|-- sweep.py        # 步进扫频、TTL 触发、启动和停止
|-- controller.py   # 将以上模块组合成统一控制器
`-- __init__.py     # 对外导出接口
```

## TTL 触发完整扫频

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    source.sweep.configure_step_sweep(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        dwell_ms=10,
        power_dbm=-20,
    )

    # TRIG1 每收到一个 TTL 上升沿，启动一次完整扫频。
    source.sweep.use_ttl_sweep_trigger("TRIG1", "POS")
    source.sweep.arm()
```

TTL 信号接到信号源后面板的 Trigger 1 输入。网线负责配置并使信号源进入等待触发状态。

## TTL 逐点扫频

若希望每个 TTL 脉冲只前进一个频点，使用：

```python
source.sweep.use_ttl_point_trigger("TRIG1", "POS")
```

## 内部触发扫频

不使用外部 TTL，执行 `arm()` 后立即扫频：

```python
source.sweep.use_internal_trigger()
source.sweep.arm()
```

停止扫频并关闭 RF：

```python
source.sweep.stop()
```

## 固定频率输出

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    source.output.set_single_point(
        frequency_mhz=1000,
        power_dbm=-10,
        rf_on=True,
    )
```

也可以分别设置频率和功率：

```python
source.output.set_frequency(1000)  # MHz
source.output.set_power(-10)       # dBm
source.output.set_rf(True)
```

`set_cw()` 仍然保留，功能与 `set_single_point()` 相同。

## 读取当前输出状态

N5171B 是单路 RF 输出信号源，当前通道状态可这样读取：

```python
with N5171B("192.168.1.100") as source:
    state = source.output.get_state()
    print(state)
```

返回值示例：

```python
{
    "frequency_mhz": 1000.0,
    "power_dbm": -10.0,
    "rf_enabled": True,
    "modulation_enabled": False,
    "frequency_mode": "CW",
    "power_mode": "FIX",
}
```

也可以单独读取：

```python
frequency_mhz = source.output.get_frequency_mhz()
power_dbm = source.output.get_power_dbm()
rf_enabled = source.output.get_rf_enabled()
```

单独控制 RF 开关：

```python
source.output.set_rf(True)
source.output.set_rf(False)
```

## 直接发送 SCPI 指令

连接模块也可以独立使用：

```python
from n5171b import ScpiConnection

scpi = ScpiConnection("192.168.1.100")
print(scpi.connect())
scpi.write("OUTP OFF")
print(scpi.query("SYST:ERR?"))
scpi.close()
```

通过统一控制器时，可使用同一个底层连接发送额外指令：

```python
source.scpi.write("OUTP OFF")
error = source.scpi.query("SYST:ERR?")
```

## 参考资料

本项目使用的 Keysight 官方手册已下载到 `references/keysight/`。资料用途、重点页码、官方来源和文件校验值见 [references/README.md](references/README.md)。
