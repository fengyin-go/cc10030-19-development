import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import StowageWorkbench from '@/views/StowageWorkbench.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 货物登记与配载批次回执共用同一个按舱位批量核对的工作台。
    { path: '/cargo', name: 'cargo', component: StowageWorkbench, props: { moduleKey: 'cargo' } },
    {
      path: '/stowage_batch',
      name: 'stowage_batch',
      component: StowageWorkbench,
      props: { moduleKey: 'stowage_batch' },
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
