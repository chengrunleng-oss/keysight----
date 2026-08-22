# Keysight N5171B Python 控制器

这是一个纯 Python、无第三方依赖的 N5171B 控制器。计算机通过网线连接信号源，使用 TCP 5025 端口发送 SCPI 指令。

## 模块结构

```text
n5171b/
|-- connection.py   # TCP 连接、SCPI 指令发送和查询
|-- output.py       # 固定频率、功率和 RF 开关
|-- list_sweep.py   # dwell 检查、线性列表扫描和单次 TTL 触发
|-- controller.py   # 组合以上独立模块
|-- configs.py      # 默认仪器 IP
`-- __init__.py     # 对外 Python 接口
```

旧的步进扫描接口已经删除。列表扫描统一通过 `source.list_sweep` 使用。

## 连接仪器

在 N5171B 前面板依次进入：

```text
Utility > I/O Config > LAN Setup > Manual Config Settings > IP Address
```

如果 `Config Type` 是 `Auto (DHCP/Auto-IP)`，连接网线并等待仪器获得地址后再查看。没有 DHCP 时，可以给仪器和电脑设置同一网段但不重复的静态地址，例如仪器使用 `192.168.10.2`、电脑使用 `192.168.10.1`，子网掩码均为 `255.255.255.0`。

确认仪器的 Socket SCPI 服务已启用：

```text
Utility > I/O Config > LAN Services Setup > SCPI Services
> Sockets SCPI: On > Proceed With Reconfiguration
```

然后连接仪器：

```python
from n5171b import N5171B

source = N5171B("192.168.1.100")
print(source.connect())
source.close()
```

## 设置并检查 dwell

单位统一为秒。下面的代码要求仪器设置 1 ms 列表 dwell，然后读取仪器实际接受的值：

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    dwell = source.list_sweep.set_and_check_dwell(0.001)
    print("请求值:", dwell.requested_s)
    print("量化值:", dwell.programmed_s)
    print("仪器回读值:", dwell.actual_s)
    print("仪器最小值:", dwell.minimum_s)
    print("是否完全相同:", dwell.exact)
```

程序会先向仪器写入手册给出的最小值 `100 us`，再读回确认。因此，这里的最小值是当前仪器实际接受并返回的值。列表 dwell 按手册给出的 `1 us` 分辨率向上量化，最大值为 `100 s`；如果仪器报错或回读值与写入值不一致，程序会抛出异常。

也可以只读取仪器对手册最小值的实际响应：

```python
minimum_dwell_s = source.list_sweep.measure_minimum_dwell()
```

## 立即完成一次线性列表扫描

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    result = source.list_sweep.run_linear_sweep(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        sweep_time_s=1.01,
    )

    print("每点实际 dwell:", result.actual_dwell_s)
    print("实际写入的 dwell 总时间:", result.programmed_dwell_time_s)
```

这个函数会完成以下操作：

1. 生成包含起始和终止频率的等间隔频率表。
2. 按 `sweep_time_s / points` 计算每点 dwell。
3. 检查 dwell 不小于仪器回读的最小 dwell，并按 1 us 分辨率量化。
4. 写入并回读完整的频率点数、dwell 点数和 dwell 值。
5. 设置 `LIST:RETR OFF`，确保扫描结束后保持在最后一个频点。
6. 设置单次扫描并立即启动；函数等待仪器报告扫描完成后才返回。

`sweep_time_s` 表示所有扫描点的 dwell 之和，不是从第一点到最后一点的精确墙钟时间：

```text
实际扫描时间 = 所有点 dwell 之和 + 各点处理、切换和稳定时间
```

因此，`sweep_time_s` 必须至少为 `points * minimum_dwell_s`。函数不修改当前固定输出功率，默认打开 RF 输出；传入 `rf_on=False` 可以让扫描过程保持 RF 关闭。

## 等待一个外部 TTL 触发

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    result = source.list_sweep.arm_linear_sweep_for_trigger(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        sweep_time_s=1.01,
        trigger_input="TRIG1",
        edge="POS",
    )

    # 此时仪器已经进入等待状态。
    # TRIG1 上的一个 TTL 上升沿会启动整张频率表。
```

这里使用两个不同层级的触发设置：

- 整次扫描触发源为外部 `TRIG1`。
- 列表内部的点触发源为 `IMM`，收到一个外部沿后，仪器按 dwell 自动扫完整张表。
- `INIT:CONT OFF` 保证只执行一次。扫完后保持最后一个频点，不会自动重新等待第二次触发。

要再次等待一个触发，需要再次调用 `arm_linear_sweep_for_trigger()`。`edge="NEG"` 可改为下降沿，`trigger_input` 还可以选择 `TRIG2` 或 `PULSE`。

中止正在运行或等待触发的扫描：

```python
source.list_sweep.abort()
```

## 固定频率和功率

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    source.output.set_single_point(
        frequency_mhz=1000,
        power_dbm=-10,
        rf_on=True,
    )

    print(source.output.get_state())
```

## 参考资料

项目使用的 Keysight 官方手册保存在 `references/keysight/`。资料用途、重点页码、官方来源和校验值见 [references/README.md](references/README.md)。列表 dwell、回扫和触发命令主要参考 SCPI Command Reference 第 84-90 页及第 242-245 页。
