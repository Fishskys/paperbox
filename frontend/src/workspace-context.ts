import { createContext, useContext } from 'react';

export const SidebarContext = createContext({ collapsed: false, toggle: () => {} });
export function useSidebar() { return useContext(SidebarContext); }
