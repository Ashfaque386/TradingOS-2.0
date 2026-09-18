'use client'

import { FormEvent, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AlertCircle, ArrowRight, LockKeyhole, Loader2, Mail, Shield, Sparkles, WifiOff } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { ApiError } from '@/lib/api'

export default function LoginPage() {
  const router = useRouter()
  const { isAuthenticated, isLoading, signIn } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [status, setStatus] = useState<'idle' | 'loading' | 'invalid' | 'network'>('idle')
  const [powerSave, setPowerSave] = useState(false)

  useEffect(() => {
    setPowerSave(window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  }, [])

  useEffect(() => {
    if (!isLoading && isAuthenticated) router.replace('/')
  }, [isLoading, isAuthenticated, router])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!email || !/^\S+@\S+\.\S+$/.test(email) || !password) {
      setStatus('invalid')
      return
    }
    setStatus('loading')
    try {
      await signIn(email, password)
      router.push('/')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setStatus('invalid')
      } else {
        setStatus('network')
      }
    }
  }

  return (
    <main className="auth-stage" data-power-save={powerSave}>
      <div className="auth-mesh" aria-hidden="true" />
      <div className="auth-particles" aria-hidden="true">{Array.from({ length: 18 }, (_, index) => <span key={index} style={{ '--i': index } as React.CSSProperties} />)}</div>
      <div className="auth-shell">
        <div className="auth-brand"><div className="auth-brand-mark"><Sparkles className="size-4" /></div><span>TRADINGOS <b>2.0</b></span></div>
        <section className="auth-card" aria-labelledby="login-title">
          <div className="auth-card-glow" aria-hidden="true" />
          <div className="relative z-10">
            <div className="mb-8"><div className="mb-4 flex size-11 items-center justify-center rounded-2xl border border-cyan-300/25 bg-cyan-300/10 text-cyan-200 shadow-[0_0_28px_rgba(34,211,238,.18)]"><Shield className="size-5" /></div><p className="eyebrow">SECURE ACCESS // NEURAL GATEWAY</p><h1 id="login-title" className="mt-3 text-3xl font-semibold tracking-[-0.04em] text-foreground">Enter the command deck.</h1><p className="mt-2 text-sm leading-6 text-muted-foreground">Authenticate to monitor markets, orchestrate agents, and keep risk within signal.</p></div>
            <form className="flex flex-col gap-5" onSubmit={handleSubmit} noValidate>
              <label className="auth-field"><span>Email address</span><div className="auth-input-wrap"><Mail className="size-4" /><input aria-label="Email address" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@tradingos.ai" autoComplete="email" /></div></label>
              <label className="auth-field"><span>Password</span><div className="auth-input-wrap"><LockKeyhole className="size-4" /><input aria-label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Enter your access key" autoComplete="current-password" /></div></label>
              {status === 'invalid' && <div className="auth-alert auth-alert-error" role="alert"><AlertCircle className="size-4 shrink-0" /><span>{email || password ? 'Invalid credentials. Check your email and password.' : 'Enter a valid email and password to continue.'}</span></div>}
              {status === 'network' && <div className="auth-alert auth-alert-network" role="alert"><WifiOff className="size-4 shrink-0" /><span>Connectivity error. The neural gateway is unreachable. Try again.</span></div>}
              <button type="submit" disabled={status === 'loading'} className="auth-submit">{status === 'loading' ? <><Loader2 className="size-4 animate-spin" />Authenticating signal...</> : <>Initialize session<ArrowRight className="size-4" /></>}</button>
            </form>
            <p className="mt-6 text-center text-sm text-muted-foreground">
              No account yet?{' '}
              <a href="/register" className="text-cyan-300 hover:text-cyan-200">Create one</a>
            </p>
          </div>
        </section>
        <p className="mt-5 text-center font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground/50">Encrypted session · No MFA configured · NSE / BSE intelligence fabric</p>
      </div>
    </main>
  )
}
