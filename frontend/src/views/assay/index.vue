<template>
  <section class="page" data-module="assay">
    <header class="page-head">
      <div>
        <h2>化验数据管理</h2>
        <p class="page-desc">同一化验编号以最近确认复检为准，历史版本原样保留；确认结论同步化验台账、样品追溯清单与报告数据面板。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="showTrace = !showTrace">
          {{ showTrace ? '返回化验台账' : '查看样品追溯清单' }}
        </button>
        <button class="btn" type="button" @click="showPanel = !showPanel">
          {{ showPanel ? '返回化验台账' : '查看报告数据面板' }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出化验数据清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 化验台账（列表投影） -->
    <template v-if="!showTrace && !showPanel">
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>化验编号</span>
          <input v-model="keyword" placeholder="按化验编号检索" />
        </label>
        <label class="filter-item">
          <span>结果状态</span>
          <select v-model="statusFilter">
            <option value="">全部</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              <button class="link" type="button" @click="openDetail(row)">
                v{{ num(row.version) }}<template v-if="num(row.latest_version) > num(row.version)"> / 最新 v{{ num(row.latest_version) }}（待确认）</template>
                <template v-else-if="num(row.history_count) > 1">（共 {{ num(row.history_count) }} 版）</template>
              </button>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="openResult(row, false)">录入结果</button>
              <button
                class="link"
                type="button"
                :disabled="!hasPendingRetest(row)"
                @click="openConfirm(row)"
              >
                确认结论
              </button>
              <button class="link" type="button" @click="openResult(row, true)">复检</button>
              <button
                class="link"
                type="button"
                :disabled="row['结果状态'] === '已确认'"
                @click="runReturn(row)"
              >
                退回修改
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 2" class="empty-state">暂无化验数据</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 个化验编号（历史版本不计入）</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 样品追溯清单（投影） -->
    <template v-else-if="showTrace">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in traceColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in traceRows" :key="String(row.id)">
            <td v-for="column in traceColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!traceRows.length">
            <td :colspan="traceColumns.length" class="empty-state">暂无已确认的化验结论，确认后将出现在样品追溯清单</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ traceRows.length }} 条样品追溯记录（仅最近确认复检）</span></footer>
    </template>

    <!-- 报告数据面板（投影） -->
    <template v-else>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in panelColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in panelRows" :key="String(row.id)">
            <td v-for="column in panelColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!panelRows.length">
            <td :colspan="panelColumns.length" class="empty-state">暂无已确认结论，确认后将写入报告数据面板</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ panelRows.length }} 条报告面板数据</span></footer>
    </template>

    <!-- 录入结果 / 复检 -->
    <div v-if="resultForm.open" class="modal-mask" @click.self="resultForm.open = false">
      <form class="modal-card" @submit.prevent="submitResultForm">
        <h3>{{ resultForm.retest ? '发起复检（产生新版本）' : '录入结果' }} · {{ resultForm.row?.['化验编号'] }}</h3>
        <p class="page-desc" v-if="resultForm.retest">
          当前结论 v{{ num(resultForm.row?.version) }} 已确认；复检会生成 v{{ num(resultForm.row?.latest_version ?? resultForm.row?.version) + 1 }}，确认前不影响台账/追溯/面板。
        </p>
        <p class="page-desc" v-else-if="(resultForm.row?.latest_version ?? 0) > (resultForm.row?.confirmed_version ?? 0)">
          存在待确认复检 v{{ resultForm.row?.latest_version }}，本次提交将修订该版本草稿。
        </p>
        <label class="filter-item"><span>化验值</span><input v-model="resultForm.values['化验值']" required /></label>
        <label class="filter-item"><span>单位</span><input v-model="resultForm.values['单位']" /></label>
        <label class="filter-item"><span>化验方法</span><input v-model="resultForm.values['化验方法']" /></label>
        <label class="filter-item"><span>化验日期</span><input v-model="resultForm.values['化验日期']" type="date" /></label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="resultForm.open = false">取消</button>
          <button class="btn primary" type="submit">提交</button>
        </div>
      </form>
    </div>

    <!-- 确认结论 -->
    <div v-if="confirmForm.open" class="modal-mask" @click.self="confirmForm.open = false">
      <form class="modal-card" @submit.prevent="submitConfirmForm">
        <h3>确认结论 · {{ confirmForm.row?.['化验编号'] }}</h3>
        <p class="page-desc">
          将确认 v{{ confirmVersion }} 的结果；确认后回写化验台账、样品追溯清单与报告数据面板。并发确认只允许一个成功。
        </p>
        <label class="filter-item">
          <span>确认版本</span>
          <select v-model="confirmForm.version">
            <option v-for="h in confirmForm.history" :key="String(h.version)" :value="num(h.version)">
              v{{ h.version }}（{{ h.status }}，{{ h['化验值'] }}）
            </option>
          </select>
        </label>
        <label class="filter-item"><span>确认人</span><input v-model="confirmForm.confirmedBy" /></label>
        <label class="filter-item"><span>备注</span><input v-model="confirmForm.remark" /></label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="confirmForm.open = false">取消</button>
          <button class="btn primary" type="submit">确认结论</button>
        </div>
      </form>
    </div>

    <!-- 详情（含历史版本） -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal-card">
        <h3>化验结果详情 · {{ detail['化验编号'] }}</h3>
        <p class="page-desc">
          当前结论 v{{ detail.version }}（{{ detail['结果状态'] }}）
          <template v-if="detail.confirmed_version">，最近确认 v{{ detail.confirmed_version }}</template>
        </p>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>元素名称</th><th>化验值</th><th>单位</th><th>化验日期</th><th>状态</th><th>确认人</th><th>确认时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="h in detail.history" :key="String(h.version)">
              <td>v{{ h.version }}<span v-if="num(h.version) === num(detail.version)"> ★</span></td>
              <td>{{ h['元素名称'] ?? '—' }}</td>
              <td>{{ h['化验值'] ?? '—' }}</td>
              <td>{{ h['单位'] ?? '—' }}</td>
              <td>{{ h['化验日期'] ?? '—' }}</td>
              <td>{{ h.status ?? '—' }}</td>
              <td>{{ h.confirmed_by ?? '—' }}</td>
              <td>{{ h.confirmed_at ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Detail = Row & { history: Row[] }

const ENDPOINT = '/api/assay'
const columns = ['化验编号', '样品编号', '元素名称', '化验值', '单位', '化验方法', '化验日期', '结果状态']
const statuses = ['待录入', '已录入', '待确认', '已确认', '已退回']
const traceColumns = ['样品编号', '化验编号', '元素名称', '化验值', '单位', '化验方法', '化验日期', '确认版本', '结论', '确认时间', '追溯状态']
const panelColumns = ['化验编号', '样品编号', '元素名称', '化验值', '单位', '化验方法', '化验日期', '确认版本', '结论', '确认人', '确认时间']

const rows = ref<Row[]>([])
const traceRows = ref<Row[]>([])
const panelRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const showTrace = ref(false)
const showPanel = ref(false)
const detail = ref<Detail | null>(null)

const stats = ref([
  { label: '化验编号数', value: 0 },
  { label: '待确认结果', value: 0 },
  { label: '已确认结论', value: 0 },
])

const resultForm = reactive<{ open: boolean; retest: boolean; row: Row | null; values: Record<string, string> }>({
  open: false,
  retest: false,
  row: null,
  values: {},
})

const confirmForm = reactive<{ open: boolean; row: Row | null; version: number; history: Row[]; confirmedBy: string; remark: string }>({
  open: false,
  row: null,
  version: 0,
  history: [],
  confirmedBy: '',
  remark: '',
})

const confirmVersion = computed(() => confirmForm.version)

function num(value: string | number | null | undefined): number {
  return Number(value ?? 0) || 0
}

function hasPendingRetest(row: Row): boolean {
  // 有待确认复检版本，或当前版本尚未确认（可首次确认）。
  return num(row.latest_version) > num(row.confirmed_version) || row['结果状态'] !== '已确认'
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('化验结果列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '化验编号数', value: total.value },
      { label: '待确认结果', value: rows.value.filter((r) => num(r.latest_version) > num(r.confirmed_version)).length },
      { label: '已确认结论', value: rows.value.filter((r) => r['结果状态'] === '已确认').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '化验数据列表读取失败'
  }
}

async function reloadProjections() {
  const [trace, panel] = await Promise.all([
    request(`${ENDPOINT}/trace`),
    request(`${ENDPOINT}/report-panel`),
  ])
  traceRows.value = trace.ok ? await trace.json() : []
  panelRows.value = panel.ok ? await panel.json() : []
}

function openResult(row: Row, retest: boolean) {
  resultForm.open = true
  resultForm.retest = retest
  resultForm.row = row
  resultForm.values = {
    '化验值': String(row['化验值'] ?? ''),
    '单位': String(row['单位'] ?? ''),
    '化验方法': String(row['化验方法'] ?? ''),
    '化验日期': String(row['化验日期'] ?? ''),
  }
}

async function submitResultForm() {
  if (!resultForm.row) return
  const requestId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  // 断连重发沿用同一 request_id 实现幂等；此处每次提交为新意图，使用新 id。
  const body = {
    values: { ...resultForm.values, retest: resultForm.retest || undefined, request_id: requestId },
  }
  try {
    const response = await request(`${ENDPOINT}/${resultForm.row.id}/results`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || payload.message || '结果提交失败')
    resultForm.open = false
    await Promise.all([reload(), reloadProjections()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果提交失败'
  }
}

async function openConfirm(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('详情读取失败')
    const data: Detail = await response.json()
    confirmForm.open = true
    confirmForm.row = row
    confirmForm.history = data.history ?? []
    confirmForm.version = num(row.latest_version) || num(row.version) || 1
    confirmForm.confirmedBy = ''
    confirmForm.remark = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

async function submitConfirmForm() {
  if (!confirmForm.row) return
  const requestId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  try {
    const response = await request(`${ENDPOINT}/${confirmForm.row.id}/confirm`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          version: confirmForm.version,
          expected_version: confirmForm.row.confirmed_version ?? 0,
          confirmed_by: confirmForm.confirmedBy || '当前值班',
          remark: confirmForm.remark || undefined,
          request_id: requestId,
        },
      }),
    })
    const payload = await response.json()
    if (response.status === 409) {
      errorMessage.value = payload.detail || '该版本已被其他请求确认，请刷新后查看最新结论'
      confirmForm.open = false
      await Promise.all([reload(), reloadProjections()])
      return
    }
    if (!response.ok) throw new Error(payload.detail || payload.message || '确认失败')
    confirmForm.open = false
    await Promise.all([reload(), reloadProjections()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function runReturn(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/return`, {
      method: 'POST',
      body: JSON.stringify({ values: { request_id: `web-${Date.now()}` } }),
    })
    const payload = await response.json()
    if (response.status === 409) {
      errorMessage.value = payload.detail || '已确认结论不能退回，请发起复检'
      return
    }
    if (!response.ok) throw new Error(payload.detail || payload.message || '退回失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '退回失败'
  }
}

async function openDetail(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('详情读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), reloadProjections()])
})
</script>
