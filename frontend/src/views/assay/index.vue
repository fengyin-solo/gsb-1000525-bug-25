<template>
  <section class="page" data-module="assay">
    <header class="page-head">
      <div>
        <h2>化验数据管理</h2>
        <p class="page-desc">同一化验编号以最近确认复检为准，历史版本只读保留；未确认复检不会覆盖台账、追溯与报告面板。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记化验结果</button>
        <button class="btn" type="button" @click="exportRows">导出化验台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>化验编号</span>
        <input v-model="filters.keyword" placeholder="按化验编号检索" />
      </label>
      <label class="filter-item">
        <span>样品编号</span>
        <input v-model="filters.sample_no" placeholder="按样品编号检索" />
      </label>
      <label class="filter-item">
        <span>结果状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>确认口径</span>
        <select v-model="filters.confirmed">
          <option value="">全部</option>
          <option value="true">仅已确认</option>
          <option value="false">仅未确认</option>
        </select>
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
          <td>
            <button class="link" type="button" @click="openHistory(row)">{{ row['化验编号'] }}</button>
          </td>
          <td v-for="column in columns.slice(1)" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              @click="openAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无化验数据，可先登记化验结果</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条化验台账记录（每条为一个化验编号的生效版本）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记 / 动作弹窗 -->
    <div v-if="modal.open" class="modal-mask" @click.self="closeModal">
      <div class="modal">
        <h3>{{ modal.title }}</h3>
        <p v-if="modal.hint" class="modal-hint">{{ modal.hint }}</p>
        <div class="form-grid">
          <label v-for="field in modalFields" :key="field.key" class="form-item">
            <span>{{ field.label }}<em v-if="field.required">*</em></span>
            <input v-if="field.type !== 'textarea'" v-model="form[field.key]" :placeholder="`请输入${field.label}`" />
            <textarea v-else v-model="form[field.key]" :placeholder="`请输入${field.label}`" rows="2" />
          </label>
        </div>
        <div class="modal-foot">
          <span v-if="modal.error" class="error-text">{{ modal.error }}</span>
          <button class="btn ghost" type="button" @click="closeModal">取消</button>
          <button class="btn primary" type="button" :disabled="modal.saving" @click="submitModal">
            {{ modal.saving ? '提交中…' : '提交' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 版本历史抽屉 -->
    <div v-if="history.open" class="modal-mask" @click.self="history.open = false">
      <div class="modal wide">
        <h3>版本历史 · {{ history.assayNo }}</h3>
        <p class="modal-hint">历史结果按原版本保留；样品追溯与报告面板只取最近确认版本。</p>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="col in historyColumns" :key="col">{{ col }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ver in history.versions" :key="Number(ver.version)">
              <td v-for="col in historyColumns" :key="col">{{ historyCell(ver, col) }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-foot">
          <button class="btn primary" type="button" @click="history.open = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type Version = Record<string, string | number | null>

const ENDPOINT = '/api/assay'
const columns = ['化验编号', '样品编号', '元素名称', '化验值', '单位', '化验方法', '化验日期', '结果状态', '确认版本']
const statuses = ['待录入', '已录入', '已确认', '已退回']
const historyColumns = ['版本', '来源', '化验值', '单位', '化验方法', '化验日期', '状态', '确认时间']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = reactive<{ keyword: string; sample_no: string; status: string; confirmed: string }>({
  keyword: '',
  sample_no: '',
  status: '',
  confirmed: '',
})
const stats = ref([
  { label: '已确认结论', value: 0 },
  { label: '待确认结果', value: 0 },
  { label: '退回待复检', value: 0 },
  { label: '涉及样品', value: 0 },
])

function actionsFor(row: Row): string[] {
  switch (row['结果状态']) {
    case '待录入':
      return ['录入结果']
    case '已录入':
      return ['确认结论', '退回修改']
    case '已确认':
    case '已退回':
      return ['提交复检']
    default:
      return []
  }
}

function resetFilters() {
  filters.keyword = ''
  filters.sample_no = ''
  filters.status = ''
  filters.confirmed = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.keyword) params.set('keyword', filters.keyword)
  if (filters.sample_no) params.set('sample_no', filters.sample_no)
  if (filters.status) params.set('status', filters.status)
  if (filters.confirmed) params.set('confirmed', filters.confirmed)
  try {
    const [listResp, panelResp] = await Promise.all([
      request(`${ENDPOINT}?${params.toString()}`),
      request(`${ENDPOINT}/report_panel`),
    ])
    if (!listResp.ok) throw new Error('化验台账读取失败')
    const payload = await listResp.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    if (panelResp.ok) {
      const panel = await panelResp.json()
      stats.value = [
        { label: '已确认结论', value: Number(panel['已确认结论'] ?? 0) },
        { label: '待确认结果', value: Number(panel['待确认结果'] ?? 0) },
        { label: '退回待复检', value: Number(panel['退回待复检'] ?? 0) },
        { label: '涉及样品', value: Number(panel['涉及样品'] ?? 0) },
      ]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '化验台账读取失败'
  }
}

// ---- 弹窗（登记 / 动作）------------------------------------------------------
type ModalField = { key: string; label: string; required?: boolean; type?: 'input' | 'textarea' }

const ACTION_FIELDS: Record<string, ModalField[]> = {
  create: [
    { key: '化验编号', label: '化验编号', required: true },
    { key: '样品编号', label: '样品编号', required: true },
    { key: '元素名称', label: '元素名称', required: true },
  ],
  录入结果: [
    { key: '化验值', label: '化验值', required: true },
    { key: '单位', label: '单位' },
    { key: '化验方法', label: '化验方法' },
    { key: '化验日期', label: '化验日期' },
  ],
  提交复检: [
    { key: '化验值', label: '复检化验值', required: true },
    { key: '单位', label: '单位' },
    { key: '化验方法', label: '化验方法' },
    { key: '化验日期', label: '化验日期' },
  ],
  确认结论: [
    { key: '确认人', label: '确认人' },
    { key: '确认结论', label: '确认结论', type: 'textarea' },
  ],
  退回修改: [{ key: '退回原因', label: '退回原因', type: 'textarea' }],
}

const modal = reactive({
  open: false,
  title: '',
  hint: '',
  action: 'create',
  assayNo: '',
  saving: false,
  error: '',
  requestId: '',
})
const form = reactive<Record<string, string>>({})
const modalFields = ref<ModalField[]>([])

function openCreate() {
  openModal('create', '', '登记化验结果', '登记后生成 v1 待录入版本；同一化验编号不能重复登记。')
}

function openAction(action: string, row: Row) {
  const assayNo = String(row['化验编号'])
  const hints: Record<string, string> = {
    录入结果: '录入后进入待确认，确认前不会影响样品追溯与报告面板。',
    提交复检: '复检生成新版本；确认前台账与追溯仍展示上一确认结论，不会被旧/新结果覆盖。',
    确认结论: '确认在同一事务回写化验台账、样品追溯清单与报告数据面板；并发确认只有一个成功。',
    退回修改: '退回后可提交复检；已确认结论不能退回，只能复检。',
  }
  openModal(action, assayNo, `${action} · ${assayNo}`, hints[action] ?? '')
}

function openModal(action: string, assayNo: string, title: string, hint: string) {
  modal.open = true
  modal.action = action
  modal.assayNo = assayNo
  modal.title = title
  modal.hint = hint
  modal.saving = false
  modal.error = ''
  // 幂等键：同一操作的网络重试/断连重放复用，后端据此回放首次结论
  modal.requestId = (globalThis.crypto?.randomUUID?.() ?? `req-${Date.now()}-${Math.random()}`)
  modalFields.value = ACTION_FIELDS[action] ?? []
  for (const key of Object.keys(form)) delete form[key]
}

function closeModal() {
  modal.open = false
}

async function submitModal() {
  modal.error = ''
  const missing = modalFields.value.filter((f) => f.required && !form[f.key]?.trim())
  if (missing.length) {
    modal.error = `缺少必填字段：${missing.map((f) => f.label).join('、')}`
    return
  }
  modal.saving = true
  try {
    const values: Record<string, string> = { action: modal.action, request_id: modal.requestId, ...form }
    const url = modal.action === 'create' ? ENDPOINT : `${ENDPOINT}/${encodeURIComponent(modal.assayNo)}/actions`
    const response = await request(url, { method: 'POST', body: JSON.stringify({ values }) })
    if (!response.ok) {
      let detail = '操作未生效，请稍后重试'
      try {
        detail = String((await response.json()).detail ?? detail)
      } catch {
        /* 保留默认提示 */
      }
      if (response.status === 409) {
        detail = `确认冲突，已确认结果未被覆盖：${detail}`
      }
      throw new Error(detail)
    }
    modal.open = false
    await reload()
  } catch (error) {
    // 保留弹窗与同一 request_id，用户直接重试即为幂等重放
    modal.error = error instanceof Error ? error.message : '操作失败'
  } finally {
    modal.saving = false
  }
}

// ---- 版本历史 ----------------------------------------------------------------
const history = reactive({ open: false, assayNo: '', versions: [] as Version[] })

async function openHistory(row: Row) {
  history.assayNo = String(row['化验编号'])
  history.versions = []
  try {
    const response = await request(`${ENDPOINT}/${encodeURIComponent(history.assayNo)}`)
    if (!response.ok) throw new Error('明细读取失败')
    const detail = await response.json()
    history.versions = (detail['历史版本'] ?? []) as Version[]
    history.open = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '化验明细读取失败'
  }
}

function historyCell(ver: Version, col: string): string {
  const map: Record<string, string> = {
    版本: `v${String(ver.version)}${ver.confirmed ? '（已确认）' : ''}`,
    来源: String(ver.source ?? ''),
    化验值: textOrDash(ver['化验值']),
    单位: textOrDash(ver['单位']),
    化验方法: textOrDash(ver['化验方法']),
    化验日期: textOrDash(ver['化验日期']),
    状态: String(ver.status ?? ''),
    确认时间: textOrDash(ver['confirmed_at']),
  }
  return map[col] ?? '—'
}

function textOrDash(value: unknown): string {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

onMounted(reload)
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  width: 460px;
  max-height: 82vh;
  overflow: auto;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.modal.wide {
  width: 860px;
}
.modal h3 {
  margin: 0 0 6px;
  font-size: 16px;
}
.modal-hint {
  color: var(--muted);
  font-size: 12px;
  margin: 0 0 12px;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.form-item em {
  color: #b42318;
  font-style: normal;
}
.form-item input,
.form-item textarea,
.filter-item select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  color: #1f2937;
}
.modal-foot {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.modal-foot .error-text {
  margin-right: auto;
  font-size: 12px;
}
</style>
