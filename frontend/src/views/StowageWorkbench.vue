<template>
  <section class="page stowage-page" :data-module="moduleKey">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <input v-model="vesselRef" class="vessel-input" data-testid="stowage-vessel" placeholder="船舶编号或船名，如 S6" />
        <button class="btn" data-testid="stowage-refresh" type="button" @click="reload">刷新余量</button>
        <template v-if="moduleKey === 'cargo'">
          <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
            登记货物
          </button>
          <button class="btn" data-testid="export" type="button" @click="exportRows">
            导出货物清单
          </button>
        </template>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">船舶</span>
        <strong class="stat-value">{{ capacity.船名 ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">载重吨</span>
        <strong class="stat-value">{{ display(capacity.载重吨) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已占用吨位</span>
        <strong class="stat-value">{{ display(capacity.已占用吨位) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">剩余载重吨</span>
        <strong class="stat-value" data-testid="stowage-remaining">{{ display(capacity.剩余载重吨) }}</strong>
      </article>
    </div>

    <p v-if="capacity.message" class="error-text">{{ capacity.message }}</p>

    <!-- 货物登记：多选货票、按舱位核对、提交配载批次 -->
    <template v-if="moduleKey === 'cargo'">
      <div class="stowage-toolbar">
        <label class="filter-item">
          <span>本次舱位</span>
          <input v-model="slotInput" data-testid="stowage-slot" placeholder="不填则使用货票舱位" />
        </label>
        <button class="btn" data-testid="stowage-preview-btn" type="button" @click="runPreview">
          核对余量
        </button>
        <button class="btn" data-testid="submit-batch" type="button" @click="submit(false)">
          提交配载批次
        </button>
        <button class="btn primary" data-testid="submit-batch-confirm" type="button" @click="submit(true)">
          提交并核对装载
        </button>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>货票</th>
            <th>品名</th>
            <th>托运方</th>
            <th>件数</th>
            <th>总吨数</th>
            <th>复磅吨数</th>
            <th>舱位</th>
            <th>状态</th>
            <th>配载资格</th>
            <th>单票操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in cargoRows"
            :key="row.id"
            :data-testid="`ticket-row-${ticketOf(row)}`"
            :data-status="String(row.status)"
          >
            <td>
              <input
                type="checkbox"
                :data-testid="`ticket-check-${ticketOf(row)}`"
                :checked="selected.includes(ticketOf(row))"
                :disabled="String(row.status) !== '待装载' && String(row.status) !== '草稿'"
                @change="toggle(ticketOf(row))"
              />
            </td>
            <td>{{ ticketOf(row) }}</td>
            <td>{{ row['货物品名'] }}</td>
            <td>{{ row['托运方'] }}</td>
            <td>{{ row['件数'] }}</td>
            <td>{{ display(row['总吨数']) }}</td>
            <td>{{ row['复磅吨数'] ?? '—' }}</td>
            <td>{{ row['舱位'] ?? '—' }}</td>
            <td>{{ row.status }}</td>
            <td :data-testid="`ticket-status-${ticketOf(row)}`">
              <span v-if="eligibility[ticketOf(row)]">
                {{ eligibility[ticketOf(row)]?.['结论'] }}
                <small class="hint">{{ eligibility[ticketOf(row)]?.['说明'] }}</small>
              </span>
              <span v-else class="hint">待核对</span>
            </td>
            <td class="row-actions">
              <button
                v-if="String(row.status) === '待装载' || String(row.status) === '草稿'"
                class="link"
                :data-testid="`confirm-cargo-${ticketOf(row)}`"
                type="button"
                @click="confirmSingle(ticketOf(row))"
              >
                单票确认
              </button>
              <button
                class="link"
                :data-testid="`reweigh-${ticketOf(row)}`"
                type="button"
                @click="reweigh(row)"
              >
                补报称重
              </button>
            </td>
          </tr>
          <tr v-if="!cargoRows.length">
            <td colspan="11" class="empty-state">暂无货票，请先在货物登记中新增</td>
          </tr>
        </tbody>
      </table>

      <section v-if="preview" class="preview-panel" data-testid="stowage-preview">
        <header class="preview-head">
          <strong>余量核对：{{ preview['船舶档案'] }}</strong>
          <span>
            载重 {{ display(preview['载重吨']) }} 吨 · 已占 {{ display(preview['已占用吨位']) }} 吨
            · 本次可装 {{ display(preview['本次可确认吨位']) }} 吨
            · 确认后占用 {{ display(preview['确认后占用']) }} 吨
            · 剩余 <b data-testid="preview-remaining">{{ display(preview['确认后剩余']) }}</b> 吨
          </span>
        </header>
        <p class="hint">{{ preview['说明'] }}</p>
        <table class="data-table">
          <thead>
            <tr><th>货票</th><th>舱位</th><th>申报吨位</th><th>本批可确认</th><th>单票可确认</th><th>结论</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in preview['明细'] ?? []" :key="item['货票编号']" :data-testid="`preview-line-${item['货票编号']}`">
              <td>{{ item['货票编号'] }}</td>
              <td>{{ item['舱位'] }}</td>
              <td>{{ display(item['申报吨位']) }}</td>
              <td>{{ item['可确认'] ? '是' : '否' }}</td>
              <td>{{ item['单票可确认'] ? '是' : '否' }}</td>
              <td>{{ item['结论'] }}</td>
              <td class="hint">{{ item['说明'] }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="(preview['需协调货票'] ?? []).length" class="warn-text" data-testid="preview-coordination">
          需原批协调货票：{{ preview['需协调货票']?.join('、') }}
        </p>
        <p v-if="(preview['余量不足货票'] ?? []).length" class="warn-text" data-testid="preview-capacity-short">
          余量不足货票：{{ preview['余量不足货票']?.join('、') }}（可等原批释放吨位或拆批提交）
        </p>
        <p v-if="(preview['本批已装载货票'] ?? []).length" class="hint">
          本批已装载（吨位已计入占用）：{{ preview['本批已装载货票']?.join('、') }}
        </p>
        <p v-if="(preview['单票可确认货票'] ?? []).length" class="hint">
          可单独确认货票：{{ preview['单票可确认货票']?.join('、') }}
        </p>
      </section>
    </template>

    <!-- 配载批次：回执列表与核对 -->
    <template v-else>
      <div class="batch-grid">
        <article
          v-for="batch in batches"
          :key="batch.id"
          class="batch-card"
          :data-testid="`batch-${batch['批次号']}`"
          :data-status="batch.status"
        >
          <header class="batch-head">
            <div>
              <strong>{{ batch['批次号'] }}</strong>
              <span class="batch-meta">船舶 {{ batch['船舶档案'] }} · 舱位 {{ batch['舱位清单'] }}</span>
            </div>
            <span class="badge">{{ batch.status }}</span>
          </header>
          <p class="hint">
            核准 {{ display(batch['核准吨位']) }} 吨 / 载重 {{ display(batch['船舶载重吨']) }} 吨 ·
            占用 {{ display(batch['已占用吨位'] ?? batch['确认后占用']) }} 吨 ·
            剩余 {{ display(batch['剩余载重吨']) }} 吨
          </p>
          <table class="data-table compact">
            <thead>
              <tr><th>货票</th><th>舱位</th><th>申报吨位</th><th>核对结果</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in batch['批次明细']" :key="item.cargo_id" :data-testid="`batch-line-${batch['批次号']}-${item['货票编号']}`">
                <td>{{ item['货票编号'] }}</td>
                <td>{{ item['舱位'] }}</td>
                <td>{{ display(item['申报吨位']) }}</td>
                <td>{{ item.result ?? item['结论'] }}</td>
                <td class="hint">{{ item['说明'] }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="(batch['需协调货票'] ?? []).length" class="warn-text">
            需原批协调货票：{{ batch['需协调货票']?.join('、') }}
          </p>
          <p v-if="(batch['余量不足货票'] ?? []).length" class="warn-text">
            余量不足货票：{{ batch['余量不足货票']?.join('、') }}
          </p>
          <p v-if="(batch['未确认货票'] ?? []).length" class="hint">
            未确认货票可回原批办理：{{ batch['未确认货票']?.join('、') }}
          </p>
          <div class="page-actions">
            <button
              class="btn primary"
              :data-testid="`confirm-batch-${batch['批次号']}`"
              type="button"
              @click="confirmBatchRow(batch)"
            >
              核对装载
            </button>
            <button
              class="btn"
              :data-testid="`return-batch-${batch['批次号']}`"
              type="button"
              @click="returnBatchRow(batch)"
            >
              退回协调
            </button>
          </div>
        </article>
        <p v-if="!batches.length" class="empty-state">暂无配载批次回执</p>
      </div>
    </template>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记货物</h3>
        <label v-for="field in createFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit">确认登记</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>{{ message }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="stowage-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { createEntry, downloadEntries, listEntries, moduleMeta } from '@/api/backend-service'
import {
  cargoEligibility,
  confirmBatch as confirmBatchApi,
  confirmCargo,
  listBatches,
  previewBatch,
  returnBatch as returnBatchApi,
  reweighCargo,
  submitBatch,
  vesselCapacity,
} from '@/api/stowage-service'
import type { CargoEligibility, StowageBatch, StowagePreview, VesselCapacity } from '@/api/stowage-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)

function display(value: unknown) {
  if (value === undefined || value === null || value === '') return '—'
  const numeric = Number(value)
  return Number.isFinite(numeric) ? String(numeric) : String(value)
}

const vesselRef = ref('S6')
const slotInput = ref('')
const cargoRows = ref<EntryRow[]>([])
const batches = ref<StowageBatch[]>([])
const selected = ref<string[]>([])
const preview = ref<StowagePreview | null>(null)
const eligibility = ref<Record<string, CargoEligibility>>({})
const capacity = ref<VesselCapacity>({ ok: false })
const message = ref('')
const errorMessage = ref('')
const showCreate = ref(false)
const createValues = ref<Record<string, string>>({})
const createFields = [
  '货票编号', '货物品名', '托运方', '件数', '总吨数', '舱位', '装载船舶', '装货港', '卸货港',
]

function openCreate() {
  createValues.value = Object.fromEntries(createFields.map((field) => [field, '']))
  createValues.value['装载船舶'] = vesselRef.value
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  if (!createValues.value['货物品名'] || !createValues.value['托运方'] || !createValues.value['件数']) {
    errorMessage.value = '货物品名、托运方、件数为必填'
    return
  }
  try {
    const result = await createEntry('cargo', createValues.value)
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    message.value = '货物已登记'
    showCreate.value = false
    await reload()
  } catch (error) {
    markError(error, '货物登记失败')
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries('cargo')
  } catch (error) {
    markError(error, '导出失败')
  }
}

function ticketOf(row: EntryRow) {
  return String(row['货票编号'] || `CGO-${String(row.id).padStart(4, '0')}`)
}

function toggle(ticket: string) {
  selected.value = selected.value.includes(ticket)
    ? selected.value.filter((item) => item !== ticket)
    : [...selected.value, ticket]
}

function markError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function loadCargo() {
  const payload = await listEntries('cargo')
  cargoRows.value = payload.items
  eligibility.value = {}
  const pending = cargoRows.value.filter((row) => ['待装载', '草稿'].includes(String(row.status)))
  await Promise.all(
    pending.map(async (row) => {
      try {
        const result = await cargoEligibility(ticketOf(row))
        if (result.ok) eligibility.value[ticketOf(row)] = result
      } catch {
        // 单票资格读取失败不阻断整页；悬停提示会显示「待核对」。
      }
    }),
  )
}

async function loadCapacity() {
  try {
    capacity.value = await vesselCapacity(vesselRef.value)
  } catch (error) {
    capacity.value = { ok: false }
    markError(error, '船舶余量读取失败')
  }
}

async function loadBatches() {
  try {
    batches.value = await listBatches()
  } catch (error) {
    markError(error, '配载批次回执读取失败')
  }
}

async function reload() {
  errorMessage.value = ''
  message.value = ''
  if (props.moduleKey === 'cargo') {
    await Promise.all([loadCargo(), loadCapacity()])
  } else {
    await Promise.all([loadBatches(), loadCapacity()])
  }
}

async function runPreview() {
  errorMessage.value = ''
  if (!vesselRef.value.trim()) {
    errorMessage.value = '请先填写船舶编号或船名'
    return
  }
  if (!selected.value.length) {
    errorMessage.value = '请至少选择一张货票'
    return
  }
  try {
    preview.value = await previewBatch(vesselRef.value.trim(), selected.value, slotInput.value.trim())
    message.value = preview.value.ok
      ? (preview.value['说明'] ?? '余量核对完成')
      : (preview.value['说明'] ?? '余量核对未通过')
  } catch (error) {
    markError(error, '配载余量核对失败')
  }
}

async function submit(confirmNow: boolean) {
  errorMessage.value = ''
  if (!selected.value.length) {
    errorMessage.value = '请至少选择一张货票'
    return
  }
  try {
    const result = await submitBatch(
      vesselRef.value.trim(), selected.value, slotInput.value.trim(), confirmNow,
    )
    message.value = result.message
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    preview.value = null
    selected.value = []
    await reload()
  } catch (error) {
    markError(error, '配载批次提交失败')
  }
}

async function confirmSingle(ticket: string) {
  errorMessage.value = ''
  try {
    const result = await confirmCargo(ticket)
    message.value = result.message
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    await reload()
  } catch (error) {
    markError(error, '货票确认装载失败')
  }
}

async function reweigh(row: EntryRow) {
  errorMessage.value = ''
  const current = Number(row['复磅吨数'] ?? row['总吨数'] ?? 0)
  const raw = window.prompt(`补报称重：${ticketOf(row)} 的复磅吨数`, String(current))
  if (raw === null) return
  const weight = Number(raw)
  if (!Number.isFinite(weight) || weight <= 0) {
    errorMessage.value = '复磅吨数必须是大于 0 的数字'
    return
  }
  try {
    const result = await reweighCargo(ticketOf(row), weight)
    message.value = result.message
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    preview.value = null
    await reload()
  } catch (error) {
    markError(error, '补报称重失败')
  }
}

async function confirmBatchRow(batch: StowageBatch) {
  errorMessage.value = ''
  try {
    const result = await confirmBatchApi(batch.id)
    message.value = result.message
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    markError(error, '配载批次核对失败')
  }
}

async function returnBatchRow(batch: StowageBatch) {
  errorMessage.value = ''
  try {
    const result = await returnBatchApi(batch.id, '货代退回原批协调')
    message.value = result.message
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    markError(error, '配载批次退回失败')
  }
}

watch(vesselRef, () => {
  void loadCapacity()
})

onMounted(reload)
</script>

