// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ api: vi.fn(), post: vi.fn(), refresh: vi.fn(), notify: vi.fn(), view: 'settings' }));
vi.mock('./store', () => ({ useWorkspace: () => ({ view: state.view, health: { model_configured: true }, refresh: state.refresh, notify: state.notify }) }));
vi.mock('./api', async importOriginal => ({ ...await importOriginal<typeof import('./api')>(), api: state.api, post: state.post }));
import { Settings } from './Tools';

const saved = { model: 'model-a', base_url: 'https://provider-a.invalid/v1', has_api_key: true, timeout_seconds: 90 };
beforeEach(() => {
  vi.clearAllMocks();
  state.view = 'settings';
  state.api.mockImplementation(async (_path: string, options?: RequestInit) => options?.method === 'PUT'
    ? { ...saved, ...JSON.parse(String(options.body)), has_api_key: false }
    : saved);
  state.refresh.mockResolvedValue(undefined);
});
afterEach(cleanup);

it('explains a changed service and saves without implicitly reusing credentials', async () => {
  render(<Settings />);
  const address = await screen.findByLabelText('API 地址');
  fireEvent.change(address, { target: { value: 'https://provider-b.invalid/v1' } });
  expect(screen.getByText(/服务地址已改变/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '测试连接' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(state.notify).toHaveBeenCalledWith(expect.stringContaining('旧密钥已清除')));
  const [, options] = state.api.mock.calls.find(([, options]) => options?.method === 'PUT')!;
  expect(JSON.parse(options.body)).toEqual({ model: 'model-a', base_url: 'https://provider-b.invalid/v1' });
  expect((screen.getByRole('button', { name: '测试连接' }) as HTMLButtonElement).disabled).toBe(true);
  expect(state.post).not.toHaveBeenCalled();
});

it('clears the saved key explicitly and makes no test request', async () => {
  render(<Settings />);
  fireEvent.click(await screen.findByRole('button', { name: '清除密钥' }));
  await waitFor(() => expect(state.refresh).toHaveBeenCalledTimes(1));
  const [, options] = state.api.mock.calls.find(([, options]) => options?.method === 'PUT')!;
  expect(JSON.parse(options.body)).toEqual({ model: 'model-a', base_url: saved.base_url, clear_api_key: true });
  expect((screen.getByRole('button', { name: '清除密钥' }) as HTMLButtonElement).disabled).toBe(true);
  expect(state.post).not.toHaveBeenCalled();
});

it('retains input after a failed save and blocks clearing with unsaved settings', async () => {
  state.api.mockImplementation(async (_path: string, options?: RequestInit) => {
    if (options?.method === 'PUT') throw new Error('Simulated save failure');
    return saved;
  });
  const { container } = render(<Settings />);
  fireEvent.change(await screen.findByLabelText('模型名称'), { target: { value: 'model-b' } });
  const key = container.querySelector<HTMLInputElement>('#model-key')!;
  fireEvent.change(key, { target: { value: 'new-fake-key' } });
  expect((screen.getByRole('button', { name: '清除密钥' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
  await screen.findByText('Simulated save failure');
  expect(key.value).toBe('new-fake-key');
  expect(state.refresh).not.toHaveBeenCalled();
});

it('does not load stale settings when returning while a save is in flight', async () => {
  let finish!: (value: typeof saved) => void;
  state.api.mockImplementation(async (_path: string, options?: RequestInit) => options?.method === 'PUT'
    ? new Promise(resolve => { finish = resolve; }) : saved);
  const view = render(<Settings />);
  fireEvent.change(await screen.findByLabelText('API 地址'), { target: { value: 'https://provider-b.invalid/v1' } });
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(finish).toBeDefined());
  state.view = 'library'; view.rerender(<Settings />);
  state.view = 'settings'; view.rerender(<Settings />);
  expect(state.api.mock.calls.filter(([, options]) => options?.method !== 'PUT')).toHaveLength(1);
  await act(async () => finish({ ...saved, base_url: 'https://provider-b.invalid/v1', has_api_key: false }));
  expect((screen.getByLabelText('API 地址') as HTMLInputElement).value).toBe('https://provider-b.invalid/v1');
  expect((screen.getByRole('button', { name: '测试连接' }) as HTMLButtonElement).disabled).toBe(true);
});

it('clears the old connection success message after editing configuration', async () => {
  state.post.mockResolvedValue({ ok: true, message: 'Fixture connection succeeded' });
  render(<Settings />);
  fireEvent.click(await screen.findByRole('button', { name: '测试连接' }));
  await screen.findByText('Fixture connection succeeded');
  await waitFor(() => expect((screen.getByLabelText('模型名称') as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'another-model' } });
  expect(screen.queryByText('Fixture connection succeeded')).toBeNull();
});
