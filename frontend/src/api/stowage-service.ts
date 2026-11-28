import { request } from './client'
import type { ActionResult } from '@/data/types'

export type StowageDetail = {
  cargo_id: number
  '货票编号': string
  '舱位': string
  '申报吨位': number
  '可确认'?: boolean
  '单票可确认'?: boolean
  '结论'?: string
  '说明': string
  '确认吨位'?: number
  '可回原批'?: boolean
  result?: string
}

export type StowageBatch = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
 '批次号': string
  '船舶档案': string
  '船舶档案编号'?: number
 '舱位清单': string
  '货票清单': string
  '批次明细': StowageDetail[]
  '核准吨位': number
  '船舶载重吨': number
  '确认前占用': number
  '确认后占用': number
  '剩余载重吨': number
  '已占用吨位'?: number
  '备注说明': string
  '未确认货票'?: string[]
  '当前核对'?: StowagePreview
  '可本批确认货票'?: string[]
  '本批已装载货票'?: string[]
  '单票可确认货票'?: string[]
  '需协调货票'?: string[]
  '余量不足货票'?: string[]
}

export type StowagePreview = {
  ok: boolean
  '说明'?: string
  船舶档案?: string
  船舶档案编号?: number
  载重吨?: number
  已占用吨位?: number
  剩余载重吨?: number
  本次可确认吨位?: number
  确认后占用?: number
  确认后剩余?: number
  明细?: StowageDetail[]
  '可本批确认货票'?: string[]
  '本批已装载货票'?: string[]
  '单票可确认货票'?: string[]
  '需协调货票'?: string[]
  '余量不足货票'?: string[]
}

export type CargoEligibility = {
  ok: boolean
  '货票编号'?: string
  cargo_id?: number
  '货票状态'?: string
  '申报吨位'?: number
  '舱位'?: string
  '船舶档案'?: string
  '载重吨'?: number
  '已占用吨位'?: number
  '剩余载重吨'?: number
  '可单独确认'?: boolean
  '需原批协调'?: boolean
  '余量不足'?: boolean
  '结论'?: string
  '说明'?: string
  '已在批次'?: string | null
  '批次状态'?: string | null
  '批次回执'?: StowageBatch[]
}

export type VesselCapacity = {
  ok: boolean
  message?: string
  船舶档案编号?: number
  船名?: string
  载重吨?: number
  已占用吨位?: number
  剩余载重吨?: number
  回执?: StowageBatch[]
}

export type TicketOption = {
  id: number
  ticket: string
  name: string
  shipper: string
  tonnage: number
  slot: string
  status: string
}

async function parse<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

export async function previewBatch(
  vessel: string,
  tickets: string[],
  slot = '',
): Promise<StowagePreview> {
  const response = await request('/api/stowage/preview', {
    method: 'POST',
    body: JSON.stringify({ 船舶档案: vessel, 货票: tickets, 舱位: slot }),
  })
  return await parse<StowagePreview>(response, '配载余量核对失败')
}

export async function submitBatch(
  vessel: string,
  tickets: string[],
  slot = '',
  confirmNow = false,
): Promise<ActionResult & { entry?: StowageBatch | null }> {
  const response = await request('/api/stowage/batches', {
    method: 'POST',
    body: JSON.stringify({ 船舶档案: vessel, 货票清单: tickets, 舱位: slot, 立即确认: confirmNow }),
  })
  return await parse(response, '配载批次提交失败')
}

export async function confirmBatch(id: number): Promise<ActionResult & { entry?: StowageBatch | null }> {
  const response = await request(`/api/stowage/batches/${id}/confirm`, { method: 'POST' })
  return await parse(response, '配载批次核对失败')
}

export async function returnBatch(id: number, remark = ''): Promise<ActionResult & { entry?: StowageBatch | null }> {
  const response = await request(`/api/stowage/batches/${id}/return`, {
    method: 'POST',
    body: JSON.stringify({ 备注说明: remark }),
  })
  return await parse(response, '配载批次退回失败')
}

export async function listBatches(): Promise<StowageBatch[]> {
  const response = await request('/api/stowage/batches')
  const payload = await parse<{ items: StowageBatch[] }>(response, '配载批次回执读取失败')
  return payload.items
}

export async function cargoEligibility(ticket: string): Promise<CargoEligibility> {
  const response = await request(`/api/stowage/cargo/${encodeURIComponent(ticket)}`)
  return await parse<CargoEligibility>(response, '货票配载资格读取失败')
}

export async function confirmCargo(ticket: string): Promise<ActionResult & { entry?: unknown }> {
  const response = await request(`/api/stowage/cargo/${encodeURIComponent(ticket)}/confirm`, {
    method: 'POST',
  })
  return await parse(response, '货票确认装载失败')
}

export async function reweighCargo(ticket: string, weight: number): Promise<ActionResult> {
  const response = await request(`/api/stowage/cargo/${encodeURIComponent(ticket)}/reweigh`, {
    method: 'POST',
    body: JSON.stringify({ 复磅吨数: weight }),
  })
  return await parse(response, '补报称重失败')
}

export async function vesselCapacity(vessel: string): Promise<VesselCapacity> {
  const response = await request(`/api/stowage/vessels/${encodeURIComponent(vessel)}`)
  return await parse<VesselCapacity>(response, '船舶余量读取失败')
}
