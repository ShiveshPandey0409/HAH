import {
  Badge as KumoBadge,
  Banner,
  Button as KumoButton,
  Dialog as KumoDialog,
  Empty,
  Input as KumoInput,
  InputArea,
  Select as KumoSelect,
} from '@cloudflare/kumo'
import { Check, CircleAlert, X } from 'lucide-react'
import {
  Children,
  createContext,
  isValidElement,
  type ButtonHTMLAttributes,
  type ChangeEvent,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useContext,
} from 'react'

const FieldLabelContext = createContext('')

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  children,
  className = '',
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg'
  loading?: boolean
}) {
  const kumoVariant = {
    primary: 'primary',
    secondary: 'secondary',
    ghost: 'ghost',
    danger: 'destructive',
  }[variant] as 'primary' | 'secondary' | 'ghost' | 'destructive'
  const kumoSize = { sm: 'sm', md: 'base', lg: 'lg' }[size] as 'sm' | 'base' | 'lg'
  return (
    <KumoButton
      className={`button button--${variant} button--${size} ${className}`}
      disabled={disabled || loading}
      variant={kumoVariant}
      size={kumoSize}
      loading={loading}
      {...props}
    >
      {children}
    </KumoButton>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  className = '',
}: {
  label: string
  hint?: string
  error?: string
  children: ReactNode
  className?: string
}) {
  return (
    <label className={`field ${className}`}>
      <span className="field__label">{label}</span>
      <FieldLabelContext.Provider value={label}>{children}</FieldLabelContext.Provider>
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  )
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  const fieldLabel = useContext(FieldLabelContext)
  const { size: nativeSize, ...inputProps } = props
  void nativeSize
  return <KumoInput size="base" aria-label={(props['aria-label'] ?? fieldLabel) || undefined} className={`input ${props.className ?? ''}`} {...inputProps} />
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const fieldLabel = useContext(FieldLabelContext)
  return <InputArea aria-label={(props['aria-label'] ?? fieldLabel) || undefined} className={`input textarea ${props.className ?? ''}`} {...props} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  const fieldLabel = useContext(FieldLabelContext)
  const items: Record<string, ReactNode> = {}
  Children.forEach(props.children, (child) => {
    if (!isValidElement(child)) return
    const option = child as ReactElement<{ value?: string; children?: ReactNode }>
    if (option.props.value != null) items[String(option.props.value)] = option.props.children
  })
  return (
    <KumoSelect
      className={`input select ${props.className ?? ''}`}
      aria-label={(props['aria-label'] ?? fieldLabel) || undefined}
      value={props.value == null ? undefined : String(props.value)}
      defaultValue={props.defaultValue == null ? undefined : String(props.defaultValue)}
      disabled={props.disabled}
      required={props.required}
      items={items}
      onValueChange={(value) => {
        props.onChange?.({ target: { value: value == null ? '' : String(value) } } as ChangeEvent<HTMLSelectElement>)
      }}
    />
  )
}

export function Badge({ tone = 'neutral', children }: { tone?: 'neutral' | 'positive' | 'warning' | 'danger' | 'accent'; children: ReactNode }) {
  const variant = { neutral: 'neutral', positive: 'success', warning: 'warning', danger: 'error', accent: 'purple' }[tone] as 'neutral' | 'success' | 'warning' | 'error' | 'purple'
  return <KumoBadge variant={variant} className={`badge badge--${tone}`}>{children}</KumoBadge>
}

export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: string; body: string; action?: ReactNode }) {
  return <Empty className="empty-state" icon={icon} title={title} description={body} contents={action} />
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'error' | 'success'; children: ReactNode }) {
  return <Banner className={`notice notice--${tone}`} variant={tone === 'error' ? 'error' : tone === 'success' ? 'secondary' : 'default'} icon={tone === 'success' ? <Check size={18} /> : <CircleAlert size={18} />}>{children}</Banner>
}

export function Modal({ open, title, children, onClose }: { open: boolean; title: string; children: ReactNode; onClose: () => void }) {
  return (
    <KumoDialog.Root open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <KumoDialog size="lg" className="modal">
        <div className="modal__header">
          <KumoDialog.Title id="modal-title">{title}</KumoDialog.Title>
          <KumoDialog.Close render={(props) => <KumoButton {...props} className="icon-button" variant="ghost" shape="square" icon={<X size={19} />} aria-label="Close dialog" />} />
        </div>
        {children}
      </KumoDialog>
    </KumoDialog.Root>
  )
}

export function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {action}
    </header>
  )
}

export function Segmented({ options, value, onChange }: { options: { value: string; label: string }[]; value: string; onChange: (value: string) => void }) {
  return (
    <div className="segmented">
      {options.map((option) => (
        <button key={option.value} type="button" className={value === option.value ? 'is-active' : ''} onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Stat({ label, value, detail, ...props }: { label: string; value: ReactNode; detail?: string } & HTMLAttributes<HTMLDivElement>) {
  return (
    <div className="stat" {...props}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  )
}
