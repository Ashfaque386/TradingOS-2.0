'use client'

// The Overview's 3D orb. It lives in its own module so the page can load it with next/dynamic: three.js
// and drei are about 250 KB gzipped, and until 2026-11-21 the landing page waited for them before it
// was interactive. The page shows a still CSS orb until this arrives.

import { Canvas, useFrame } from '@react-three/fiber'
import { Float, OrbitControls, Sparkles } from '@react-three/drei'
import { useMemo } from 'react'
import type { Mesh } from 'three'

export type PulseState = 'IDLE' | 'RESEARCHING' | 'EXECUTING' | 'AWAITING APPROVAL' | 'RISK ALERT'

function PulseOrb({ state, color, reduceMotion }: { state: PulseState; color: string; reduceMotion: boolean }) {
  const mesh = useMemo(() => ({ current: null as Mesh | null }), [])
  useFrame((_, delta) => {
    // In reduced-motion / power-save mode the orb holds a static pose — the state
    // color and the text status pill still communicate the current state.
    if (reduceMotion || !mesh.current) return
    mesh.current.rotation.y += delta * (state === 'RISK ALERT' ? 1.8 : state === 'RESEARCHING' ? 0.8 : 0.28)
    mesh.current.rotation.x = Math.sin(Date.now() / 1300) * 0.08
    const pulse = state === 'AWAITING APPROVAL' ? 1 + Math.sin(Date.now() / 260) * .08 : state === 'RISK ALERT' ? 1 + Math.sin(Date.now() / 110) * .045 : 1
    mesh.current.scale.setScalar(pulse)
  })
  return (
    <group>
      <Float enabled={!reduceMotion} speed={state === 'RISK ALERT' ? 4 : 1.2} rotationIntensity={.3} floatIntensity={.35}>
        <mesh ref={mesh}>
          <icosahedronGeometry args={[1.35, 5]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={state === 'RISK ALERT' ? 2.2 : 1.2} metalness={.75} roughness={.2} wireframe={state === 'RESEARCHING'} />
        </mesh>
      </Float>
      <mesh scale={1.58}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshBasicMaterial color={color} transparent opacity={.07} />
      </mesh>
      {/* Particle flourishes are decorative — dropped in reduced-motion / power-save. */}
      {!reduceMotion && (
        <Sparkles count={state === 'RISK ALERT' ? 180 : 90} scale={state === 'RISK ALERT' ? 5.2 : 4.2} size={state === 'RISK ALERT' ? 4 : 2.4} speed={state === 'EXECUTING' ? 2 : state === 'RISK ALERT' ? 4 : .55} color={color} />
      )}
      <pointLight color={color} intensity={state === 'RISK ALERT' ? 9 : 5} distance={6} />
    </group>
  )
}

export default function PulseScene({ state, color, reduceMotion }: { state: PulseState; color: string; reduceMotion: boolean }) {
  return <Canvas frameloop={reduceMotion ? 'demand' : 'always'} camera={{ position: [0, 0, 5.4], fov: 42 }} dpr={[1, 1.5]}><ambientLight intensity={.5} /><PulseOrb state={state} color={color} reduceMotion={reduceMotion} /><OrbitControls enableZoom={false} enablePan={false} autoRotate={!reduceMotion} autoRotateSpeed={.35} /></Canvas>
}
