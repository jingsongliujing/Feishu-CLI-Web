# 稳定任务 recipes

## 会议日程

用户说：“添加会议日程，明天下午三点开会，参与人：刘劲松，黄云”

稳定链路：

1. `lark-cli contact +search-user --query "刘劲松"`
2. `lark-cli contact +search-user --query "黄云"`
3. `lark-cli calendar +create --summary "会议" --start "<明天15:00+08:00>" --end "<明天16:00+08:00>" --attendee-ids "<ou_刘劲松>,<ou_黄云>" --as user`

## 建群

用户说：“把我和黄云，赵鹤翔拉个群：名字叫飞书CLI测试群”

稳定链路：

1. 搜索黄云。
2. 搜索赵鹤翔。
3. `lark-cli im +chat-create --name "飞书CLI测试群" --user-ids "<ou_黄云>,<ou_赵鹤翔>" --as user`

当前登录用户通常由飞书 CLI 默认加入，不要给“我”编造 open_id。

## 发私信

用户说：“帮我发个消息给赵鹤翔：明天下午开会”

稳定链路：

1. 搜索赵鹤翔。
2. `lark-cli im +messages-send --user-id "<ou_赵鹤翔>" --text "明天下午开会" --as user`

## 创建云文档

用户说：“创建一个名为'今天的十条AI新闻'的云文档，并将今天的十条AI新闻内容写入其中”

稳定链路：

1. 生成简洁 markdown 内容。
2. `lark-cli docs +create --title "今天的十条AI新闻" --markdown "<markdown>"`

## Excel 导入多维表格并发群

用户说：“创建一个名为'抖店-京东热店排名'的多维表格，并将./docs/data_all_20260329_201138.xlsx上传'抖店-京东热店排名'，最后把多维表格发送到"飞书CLI测试群"”

稳定链路：

1. `lark-cli drive +import --file "./docs/data_all_20260329_201138.xlsx" --type bitable --name "抖店-京东热店排名" --as user`
2. 如果返回 `next_command`，继续执行。
3. 提取最终多维表格链接。
4. 搜索 `飞书CLI测试群`。
5. `lark-cli im +messages-send --chat-id "<oc_群>" --text "抖店-京东热店排名 多维表格已创建：<url>" --as user`

## 群里找共同空闲时间并发会议号

用户说：“帮我看一下【飞书CLI测试群】群里所有人的日历，然后4.17号找一个大家都合适的时间开一小时的讨论会，并把会议号发在群里”

稳定链路：

1. 搜索群拿 `oc_`。
2. `lark-cli calendar +suggestion --start "2026-04-17T09:00:00+08:00" --end "2026-04-17T18:00:00+08:00" --attendee-ids "<oc_群>" --duration-minutes 60 --timezone Asia/Shanghai --format json`
3. 用推荐时段创建带 `vchat.vc_type=vc` 的日程。
4. 把群作为 attendee 加入日程。
5. 提取 `meeting_url` / 会议号。
6. 发送群消息，必须包含会议时间、会议号或会议链接。

## 多人找共同空闲时间并分别通知

用户说：“4.17号我要和杨继涛，黄云开个会，找一个大家都合适的时间开一小时的讨论会，把会议信息分别发送给他们”

稳定链路：

1. 搜索杨继涛。
2. 搜索黄云。
3. `calendar +suggestion` 查询共同空闲。
4. 创建带飞书视频会议的日程。
5. 分别给杨继涛、黄云发送文本消息。
