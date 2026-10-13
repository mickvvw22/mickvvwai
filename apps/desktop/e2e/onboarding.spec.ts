/**
 * E2E first-run tests — verify the main interface opens when no inference
 * provider is configured yet.
 *
 * Launches the app with an empty config.yaml (no providers). The renderer
 * should open the main chat directly; a provider can be added later from
 * Settings or the model picker.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from './test'

import {
  type NoProviderFixture,
  setupNoProvider
} from './fixtures'

let fixture: NoProviderFixture | null = null

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('first boot with no provider configured', () => {
  test('opens the main chat directly with the five fixed titlebar controls', async () => {
    fixture = await setupNoProvider()

    await expect(fixture.page.locator('[contenteditable="true"]').first()).toBeVisible({ timeout: 90_000 })
    const rightCluster = fixture.page.locator('[data-titlebar-cluster="right"]')

    await expect(rightCluster).toBeVisible()
    await expect(rightCluster.locator('button')).toHaveCount(5)
    await expect(fixture.page.locator('[data-glass-opaque]')).toHaveCount(0)
  })
})
