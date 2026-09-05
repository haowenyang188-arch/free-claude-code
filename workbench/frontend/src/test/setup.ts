import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

// vitest globals:false 时 RTL 不会自动 cleanup，必须显式注册
afterEach(() => {
  cleanup()
})
