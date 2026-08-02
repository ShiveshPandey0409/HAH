import { UsersThree } from '@phosphor-icons/react'

interface LogoProps {
  size?: number
  className?: string
}

export function Logo({ size = 32, className }: LogoProps) {
  return (
    <div className={['logo', className].filter(Boolean).join(' ')} aria-label="Hire a Human">
      <UsersThree className="logo__mark" size={size} weight="fill" aria-hidden="true" />
      <span className="logo__label">Hire a Human</span>
    </div>
  )
}
