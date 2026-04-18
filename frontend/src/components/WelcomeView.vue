<script setup lang="ts">
defineProps<{ currentSkill: string }>()
const emit = defineEmits<{ quickQuestion: [question: string] }>()

const examples = [
  '添加会议日程，明天下午三点开会，参与人：xxx，xxx',
  '把我和xxx，xxx拉个群：名字叫飞书CLI测试群',
  '帮我发个消息给xxx：明天下午开会',
  "创建一个名为'今天的十条AI新闻'的云文档，并将今天的十条AI新闻内容写入其中",
  "创建一个名为'今天的销售数据汇总'的多维表格，并将我组织里的人写入其中",
  "创建一个名为'xx排名'的多维表格，并将./docs/xxx.xlsx上传'抖店-京东热店排名'，最后把多维表格发送到\"飞书CLI测试群\"",
  '帮我看一下【飞书CLI测试群】群里所有人的日历，然后下周找一个大家都合适的时间开一小时的讨论会',
  '然后4.17号我要和xxx，xxx开个会，找一个大家都合适的时间开一小时的讨论会，把会议信息分别发送给他们',
  '创建一个名为“本周飞书CLI测试记录”的云文档，写入今天完成的测试事项、失败原因和下一步计划',
  '下周帮我找一个我、xxx、xxx都空的一小时开复盘会，并把会议时间发到飞书CLI测试群'
]
</script>

<template>
  <div class="welcome">
    <div class="welcome-badge">Feishu CLI</div>
    <h1>有什么飞书事项需要处理？</h1>
    <p>选择“飞书CLI”后可以直接操作消息、日历、文档、云空间、表格、多维表格等能力。</p>
    <div class="quick-grid">
      <button type="button" v-for="item in examples" :key="item" @click="emit('quickQuestion', item)">
        {{ item }}
      </button>
    </div>
  </div>
</template>
