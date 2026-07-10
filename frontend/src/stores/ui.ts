import { create } from "zustand";
export const useUiStore = create<{ collapsed: boolean; toggle: () => void }>((set) => ({ collapsed: false, toggle: () => set((state) => ({ collapsed: !state.collapsed })) }));
