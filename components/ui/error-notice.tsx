/** A failed load, said in words, with a way to try again. Replaces a spinner that never ends. */
export function ErrorNotice({
  message,
  onRetry,
  className = '',
}: {
  message: string
  onRetry?: () => void
  className?: string
}) {
  return (
    <div
      role="alert"
      className={`flex items-start justify-between gap-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200 ${className}`}
    >
      <span>{message}</span>
      {onRetry && (
        <button type="button" onClick={onRetry} className="button-secondary shrink-0">
          Try again
        </button>
      )}
    </div>
  )
}
