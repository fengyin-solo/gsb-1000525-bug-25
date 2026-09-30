<template>
  <section class="page" data-module="sample_registry">
    <header class="page-head">
      <div>
        <h2>样品登记管理</h2>
        <p class="page-desc">维护送检样品，围绕送检编号、样品名称、采样位置、检测项目做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记送检样品</button>
        <button class="btn" type="button" @click="exportRows">导出样品登记清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无样品登记数据，可先登记送检样品</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条样品登记记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="trace-block">
      <header class="trace-head">
        <div>
          <h3>样品追溯清单</h3>
          <p class="page-desc">化验确认结论在同一事务回写：同一化验编号只出现最近确认复检版本，未确认复检与历史版本不会重复显示。</p>
        </div>
        <form class="trace-filter" @submit.prevent="loadTrace">
          <input v-model="traceFilter" placeholder="按样品编号/化验编号检索" />
          <button class="btn" type="submit">检索</button>
          <button class="btn ghost" type="button" @click="traceFilter = ''; loadTrace()">重置</button>
        </form>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in traceColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in traceRows" :key="`${String(row['化验编号'])}-${idx}`">
            <td v-for="column in traceColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!traceRows.length">
            <td :colspan="traceColumns.length" class="empty-state">暂无已确认化验结论，确认后自动回写到这里</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ traceRows.length }} 条已确认追溯记录</span>
        <span v-if="traceError" class="error-text">{{ traceError }}</span>
      </footer>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/sample_registry'
const columns = ["送检编号", "样品名称", "采样位置", "检测项目", "送检单位", "收样日期", "检测周期", "送检状态"]
const actions = ["确认收样", "登记报告", "退回样品"]
const statuses = ["待收样", "已收样", "检测中", "已出报告"]
const stats = [{"label": "待收样品", "value": 0}, {"label": "检测中样品", "value": 0}, {"label": "已出报告", "value": 0}]

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
  errorMessage.value = '送检样品登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('样品登记动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样品登记操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('送检样品列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样品登记列表读取失败'
  }
}

// ---- 样品追溯清单（数据来自化验内核的已确认投影） ------------------------------
type TraceRow = Record<string, string | number | null>
const traceColumns = ['样品编号', '化验编号', '元素名称', '化验值', '单位', '化验方法', '化验日期', '确认版本', '确认时间']
const traceRows = ref<TraceRow[]>([])
const traceFilter = ref('')
const traceError = ref('')

async function loadTrace() {
  traceError.value = ''
  const params = new URLSearchParams()
  if (traceFilter.value) params.set('keyword', traceFilter.value)
  try {
    const response = await request(`/api/sample_registry/traceability?${params.toString()}`)
    if (!response.ok) throw new Error('样品追溯清单读取失败')
    const payload = await response.json()
    traceRows.value = payload.items ?? []
  } catch (error) {
    traceError.value = error instanceof Error ? error.message : '样品追溯清单读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadTrace()
})
</script>

<style scoped>
.trace-block {
  margin-top: 24px;
}
.trace-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  margin-bottom: 8px;
}
.trace-filter {
  display: flex;
  gap: 8px;
  align-items: center;
}
.trace-filter input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
}
</style>
