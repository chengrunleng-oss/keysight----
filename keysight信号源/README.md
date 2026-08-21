# Keysight N5171B Python 控制器

这是一个纯 Python、无第三方依赖的 N5171B 控制器。计算机通过网线连接信号源，使用 `TCP 5025` 端口发送 SCPI 指令。

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
    source.output.set_cw(frequency_mhz=1000, power_dbm=-10)
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
