# AX9000 多线路 NSS 整形开发计划

目标是每条已有 IPv4 出口独立限速和调速，保留现有 mwan3 选路。当前可用基线为 VEPA macvlan 加逐路 CPU CAKE。实验先在这个基线上验证 NSS 队列能否按线路计数，再单独评估 ECM 卸载。NSS FQ-CoDel 提供流级排队，CAKE 的 NAT 后主机公平性需要另外评估。

## 1. 下行分类原型

`tools/nss_ingress_plan.py` 输出候选 tc 批次供检查。输入是线路编号、线路自己的上游 IPv4 地址及下载速率。只有独立实验 WAN 和已通过 nssmirred 注册的专用 IFB 适合加载新树。已有 SQM 的 IFB 上不要追加此批次。工具仅输出文本，未接入开机流程。示例地址取自文档网段。

```sh
python tools/nss_ingress_plan.py ifb-test --default-kbit 2000 \
  --line 0 192.0.2.2 30000 --line 1 192.0.2.3 45000
```

每个 nsshtb 子类挂独立 nssfq_codel 叶子，另留一个默认叶子承接未匹配报文。IFB 根上的 u32 过滤器按目的 IPv4 匹配，数据面须核实此处可见目的地址确为 DNAT 以前的出口地址。驱动 nss_qdisc_iterate_fl 使用分类结果的高 16 位，因而匹配结果使用不同叶子的 major handle。两个流若都以 1:x 返回，进入该逻辑后高位相同。槽号固定叶子编号，DHCP 地址改变时应更新过滤器。--change-slot 只生成既有单个子类的 rate/crate 更新。默认类限速、线路限速都由调用者给出，突发量按 5 ms 取整并至少 2048 字节，属于首轮实验参数。

## 2. 上行和分类验收

工具 nss_leaf_check.py 接收同一设备两份 tc -s qdisc 输出，按叶子读取字节与包数。--monitored 指定两路槽号，--active 指定本次实际发包的槽。活动路必须增长，未发包路必须保持；缺叶子、计数回退和串入另一叶子的流量都判失败。先停止实验线路的背景流量再取样。该工具只判定计数归属，速率与延迟仍需按实验时长及外部流量端点测量。

固定源码 nss-clients 51be82d 的 nss_qdisc.c 在 5.4 及更新内核只为 IFB 分配 tcf_block。物理 WAN 上的 NSS 根不能照搬下行过滤器。上行候选是在选路完成后、报文从逻辑出口转交 WAN 前写叶子优先级标签，保留 mwan3 的 fwmark 和优先级低位。先核对实际调用顺序与 pcap，再验证物理 WAN 叶子计数。提前卸载的连接还要分别核查 ECM 的正反向 QoS 标签；此项不作为普通 VEPA 转发的先决条件。

已实现的实验入口是 python -m multidial.nss PROFILE.json，输出 setup、IGS 绑定、上传与下载的 tc 批次、上行 nft 批次及各线路 autorate 配置片段。PROFILE 包含 physical、ifb、default_download_kbit、default_upload_kbit，以及 uplinks 数组，每个元素包含 slot、device、address、download_kbit、upload_kbit。所有接口名、槽号、IPv4 地址和速率先检查完整性。此入口仅生成计划。--rate SLOT DOWNLOAD_KBIT UPLOAD_KBIT 输出指定线路两个已有限速类的更新。

上传 nft 规则在 postrouting 的 -140 优先级按 oifname 匹配逻辑出口，保留 priority 低 16 位，在高位写对应叶子标签。它不写 meta mark。示例两路规则已在亚瑟通过 nft -c 检查，真实规则未加载，检查前后的 EasyTier 探测均通过。此结果只验证规则能被该内核接受。

每路 autorate 片段显式绑定该 dial 的探测接口和 RX/TX 字节路径，避免多个实例读取共享 IFB 的总流量。NSS class 调速适配器随 APK 提供，配置需最后 source 该适配器，并自行填写经过测试的上下限及反射器。tc 失败时保留 last_shaper_rate，允许控制器下次重试。默认服务仍使用现有 CAKE 路径，实验模块尚未接入 daemon 的 DHCP/上下线协调。

现场先盘点独立端口、默认路由、桥成员和 ET 的实际承载接口。只有测试流量能独立进出、队列与现网隔离时才加载实验树。记录测试前后每个叶子的字节/包数，分别做只走 A、只走 B、双路并发及未分类流量四组对照；单独改 A 的速率时，A 实测限速应变化，B 的设置及稳定吞吐应保持。上、下行分别验收，另测长连接与新连接；总字节数不能替代逐叶归属证据。亚瑟上的结果须在 AX9000 的目标内核和 NSS 版本复验。

## 3. 接入 multidial 与 autorate

双向分类通过后，将已存在的线路状态映射到稳定的类编号，把绑定该线路的时延探测反馈给该线路的 class change。增加地址更新、掉线、重连、进程重启和重复配置的幂等测试。上线前还要验证半途失败能撤回本功能创建的对象、重启后配置恢复、原有逐路 CAKE 可以切回，以及多路负载下的吞吐、延迟与 CPU。该计划未改动现网默认后端或现有固件镜像。
