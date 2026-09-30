<template>
  <section class="page" data-module="geological_report">
    <header class="page-head">
      <div>
        <h2>地质报告管理</h2>
        <p class="page-desc">维护勘探报告，围绕报告编号、勘探区、报告类型、编制人做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记勘探报告</button>
        <button class="btn" type="button" @click="exportRows">导出地质报告清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无地质报告数据，可先登记勘探报告</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条地质报告记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="report-panel">
      <header class="page-head">
        <div>
          <h3>报告数据面板 · 化验确认结论</h3>
          <p class="page-desc">化验确认结论在同一事务回写本面板：同一化验编号以最近确认复检为准，历史版本不参与统计与展示。</p>
        </div>
        <button class="btn" type="button" @click="loadAssayPanel">刷新面板</button>
      </header>
      <div class="stat-row">
        <article v-for="card in assayCards" :key="card.label" class="stat-card">
          <span class="stat-label">{{ card.label }}</span>
          <strong class="stat-value">{{ card.value }}</strong>
        </article>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in assayColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in assayItems" :key="`${String(row['化验编号'])}-${idx}`">
            <td v-for="column in assayColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!assayItems.length">
            <td :colspan="assayColumns.length" class="empty-state">暂无已确认化验结论</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/geological_report'
const columns = ["报告编号", "勘探区", "报告类型", "编制人", "审核人", "提交日期", "审定结论", "报告状态"]
const actions = ["提交内审", "提交外审", "确认定稿"]
const statuses = ["编制中", "待内审", "待外审", "已定稿", "已退回"]
const stats = [{"label": "编制中报告", "value": 0}, {"label": "待审报告", "value": 0}, {"label": "已定稿报告", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '勘探报告登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('地质报告动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '地质报告操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('勘探报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '地质报告列表读取失败'
  }
}

// ---- 报告数据面板：化验已确认结论投影 ------------------------------------------
type AssayRow = Record<string, string | number | null>
const assayColumns = ['样品编号', '化验编号', '元素名称', '化验值', '单位', '化验日期', '确认版本', '确认时间']
const assayItems = ref<AssayRow[]>([])
const assayCards = ref([
  { label: '已确认结论', value: 0 },
  { label: '待确认结果', value: 0 },
  { label: '退回待复检', value: 0 },
  { label: '涉及样品', value: 0 },
])

async function loadAssayPanel() {
  try {
    const response = await request('/api/assay/report_panel')
    if (!response.ok) throw new Error('报告数据面板读取失败')
    const panel = await response.json()
    assayCards.value = [
      { label: '已确认结论', value: Number(panel['已确认结论'] ?? 0) },
      { label: '待确认结果', value: Number(panel['待确认结果'] ?? 0) },
      { label: '退回待复检', value: Number(panel['退回待复检'] ?? 0) },
      { label: '涉及样品', value: Number(panel['涉及样品'] ?? 0) },
    ]
    assayItems.value = panel.items ?? []
  } catch {
    // 面板是报告页的辅助区块，失败不阻断报告主列表
  }
}

onMounted(() => {
  void reload()
  void loadAssayPanel()
})
</script>

<style scoped>
.report-panel {
  margin-top: 24px;
}
</style>
