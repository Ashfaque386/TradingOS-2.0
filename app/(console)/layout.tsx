import { ShellLayout } from '@/components/shell/shell-layout'

// Every signed-in page lives in this route group, so the shell (top bar, sidebar, its 30 s poll and
// the kill-switch state) is mounted once and survives navigation instead of restarting on every page.
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return <ShellLayout>{children}</ShellLayout>
}
