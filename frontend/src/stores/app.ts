import { create } from 'zustand'

export type TabId = 'dashboard' | 'loop' | 'erp' | 'rpa' | 'fde' | 'aigc'

export const TAB_ORDER: TabId[] = ['dashboard', 'loop', 'erp', 'rpa', 'fde', 'aigc']

interface AppState {
  authed: boolean
  me: { username: string; roles: string[]; must_change_password?: boolean } | null
  tab: TabId
  setAuthed: (v: boolean) => void
  setMe: (me: AppState['me']) => void
  setTab: (t: TabId) => void
}

export const useApp = create<AppState>((set) => ({
  authed: false,
  me: null,
  tab: 'dashboard',
  setAuthed: (v) => set({ authed: v }),
  setMe: (me) => set({ me, authed: me != null }),
  setTab: (t) => set({ tab: t }),
}))
