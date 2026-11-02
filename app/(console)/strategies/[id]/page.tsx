'use client'

import { Suspense, use } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useAuth } from '@/components/auth/auth-provider'
import { StrategyReview } from '@/components/strategies/review'
import { can } from '@/lib/permissions'

function Workspace({ id }: { id: string }) {
  const router = useRouter()
  const tab = useSearchParams().get('tab')
  const { role } = useAuth()
  const canOperate = can(role, 'POST /api/v1/strategies')
  const canSignOffLive = can(role, 'POST /api/v1/strategies/{strategy_id}/approve-live-eligibility')
  return <StrategyReview key={id} strategyId={id} initialTab={tab} onBack={() => router.push('/strategies')} canOperate={canOperate} canSignOffLive={canSignOffLive} />
}

/** One strategy's review workspace at an address of its own (`/strategies/{id}`, `?tab=Versions`), so it can be linked to and reloaded. */
export default function StrategyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  return (
    <Suspense fallback={<div className="mx-auto w-full max-w-[1700px] p-8 text-sm text-muted-foreground">Loading strategy…</div>}>
      <Workspace id={id} />
    </Suspense>
  )
}
