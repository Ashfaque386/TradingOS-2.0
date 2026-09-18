'use client'

import { FormEvent, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AlertCircle, ArrowRight, LockKeyhole, Loader2, Mail, ShieldPlus, Sparkles, WifiOff } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { ApiError } from '@/lib/api'

type Status = 'idle' | 'loading' | 'invalid' | 'conflict' | 'network'

export default function RegisterPage() {
  const router = useRouter()
  const { isAuthenticated, isLoading, signUp } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [powerSave, setPowerSave] = useState(false)

  useEffect(() => {
    setPowerSave(window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  }, [])

  useEffect(() => {
    if (!isLoading && isAuthenticated) router.replace('/')
  }, [isLoading, isAuthenticated, router])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!email || !/^\S+@\S+\.\S+$/.test(email) || password.length < 8 || password !== confirmPassword) {
      setStatus('invalid')
      return
    }
    setStatus('loading')
    try {
      await signUp(email, password)
      router.push('/')
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setStatus('conflict')
      } else if (err instanceof ApiError) {
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
        <section className="auth-card" aria-labelledby="register-title">
          <div className="auth-card-glow" aria-hidden="true" />
          <div className="relative z-10">
            <div className="mb-8">
              <div className="mb-4 flex size-11 items-center justify-center rounded-2xl border border-cyan-300/25 bg-cyan-300/10 text-cyan-200 shadow-[0_0_28px_rgba(34,211,238,.18)]"><ShieldPlus className="size-5" /></div>
              <p className="eyebrow">SECURE ACCESS // NEURAL GATEWAY</p>
              <h1 id="register-title" className="mt-3 text-3xl font-semibold tracking-[-0.04em] text-foreground">Provision an account.</h1>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                The very first account on a fresh system becomes a System Administrator automatically. Every account after that starts read-only until an administrator raises its role.
              </p>
            </div>
            <form className="flex flex-col gap-5" onSubmit={handleSubmit} noValidate>
              <label className="auth-field"><span>Email address</span><div className="auth-input-wrap"><Mail className="size-4" /><input aria-label="Email address" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@tradingos.ai" autoComplete="email" /></div></label>
              <label className="auth-field"><span>Password</span><div className="auth-input-wrap"><LockKeyhole className="size-4" /><input aria-label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="At least 8 characters" autoComplete="new-password" /></div></label>
              <label className="auth-field"><span>Confirm password</span><div className="auth-input-wrap"><LockKeyhole className="size-4" /><input aria-label="Confirm password" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} placeholder="Re-enter your password" autoComplete="new-password" /></div></label>
              {status === 'invalid' && (
                <div className="auth-alert auth-alert-error" role="alert">
                  <AlertCircle className="size-4 shrink-0" />
                  <span>
                    {!email || !/^\S+@\S+\.\S+$/.test(email)
                      ? 'Enter a valid email address.'
                      : password.length < 8
                        ? 'Password must be at least 8 characters.'
                        : password !== confirmPassword
                          ? 'Passwords do not match.'
                          : 'Could not create the account. Check the details and try again.'}
                  </span>
                </div>
              )}
              {status === 'conflict' && <div className="auth-alert auth-alert-error" role="alert"><AlertCircle className="size-4 shrink-0" /><span>An account with this email already exists. Sign in instead, or ask an administrator to reset it.</span></div>}
              {status === 'network' && <div className="auth-alert auth-alert-network" role="alert"><WifiOff className="size-4 shrink-0" /><span>Connectivity error. The neural gateway is unreachable. Try again.</span></div>}
              <button type="submit" disabled={status === 'loading'} className="auth-submit">{status === 'loading' ? <><Loader2 className="size-4 animate-spin" />Provisioning account...</> : <>Create account<ArrowRight className="size-4" /></>}</button>
            </form>
            <p className="mt-6 text-center text-sm text-muted-foreground">
              Already have an account?{' '}
              <a href="/login" className="text-cyan-300 hover:text-cyan-200">Sign in</a>
            </p>
          </div>
        </section>
        <p className="mt-5 text-center font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground/50">Encrypted session · No MFA configured · NSE / BSE intelligence fabric</p>
      </div>
    </main>
  )
}
