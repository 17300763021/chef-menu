# Chef Menu Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a GitHub Pages-ready dual-chef menu website with fixed recipes, daily recommendations, menu history, admin login, and photo uploads.

**Architecture:** React pages consume a repository interface. The production repository uses Supabase while a seeded local repository keeps the site demonstrable when cloud tables are empty or temporarily unavailable. Pure date, search, recommendation, and menu functions are tested independently before UI integration.

**Tech Stack:** React 19, TypeScript, Vite, React Router, Supabase JS, Vitest, Testing Library, CSS.

---

### Task 1: Test foundation and domain rules

**Files:**
- Modify: `package.json`
- Modify: `vite.config.ts`
- Create: `src/test/setup.ts`
- Create: `src/domain/types.ts`
- Create: `src/domain/menu.ts`
- Test: `src/domain/menu.test.ts`

- [ ] Install Vitest, jsdom, Testing Library, and user-event.
- [ ] Write failing tests for Shanghai date labels, ingredient search, weekday/weekend recommendations, stable daily quotes, and duplicate-free two-dish menus.
- [ ] Run the tests and confirm they fail because the domain functions do not exist.
- [ ] Implement the smallest pure functions that pass.
- [ ] Run the full test suite.

### Task 2: Repository and fallback data

**Files:**
- Create: `src/data/demoData.ts`
- Create: `src/data/repository.ts`
- Create: `src/data/supabaseRepository.ts`
- Create: `src/lib/supabase.ts`
- Test: `src/data/repository.test.ts`

- [ ] Write failing repository tests for recipe retrieval, menu persistence, completion records, and fallback behavior.
- [ ] Implement an in-memory repository backed by localStorage.
- [ ] Implement a Supabase repository matching the same interface.
- [ ] Add a resilient repository wrapper that reads Supabase first and uses demo data when remote data is unavailable or empty.
- [ ] Run repository and domain tests.

### Task 3: Application state and routing

**Files:**
- Create: `src/app/AppContext.tsx`
- Create: `src/app/AppShell.tsx`
- Modify: `src/main.tsx`
- Modify: `src/App.tsx`
- Test: `src/app/AppContext.test.tsx`

- [ ] Write failing tests for chef switching, adding/removing menu items, and persisted current menu.
- [ ] Implement context actions over the repository.
- [ ] Add HashRouter routes for home, recipes, recipe detail, today menu, history, and admin.
- [ ] Verify state tests.

### Task 4: Public pages

**Files:**
- Create: `src/components/ChefSwitcher.tsx`
- Create: `src/components/RecipeCard.tsx`
- Create: `src/components/EmptyState.tsx`
- Create: `src/pages/HomePage.tsx`
- Create: `src/pages/RecipesPage.tsx`
- Create: `src/pages/RecipeDetailPage.tsx`
- Create: `src/pages/TodayMenuPage.tsx`
- Create: `src/pages/HistoryPage.tsx`
- Test: `src/pages/public-pages.test.tsx`

- [ ] Write failing page tests for chef themes, two recommendations, category/search filtering, fixed tutorial content, menu actions, and history details.
- [ ] Implement each page with accessible controls and loading/error states.
- [ ] Run public page tests.

### Task 5: Admin and uploads

**Files:**
- Create: `src/pages/AdminPage.tsx`
- Create: `src/features/auth.ts`
- Create: `src/features/image.ts`
- Test: `src/features/image.test.ts`
- Test: `src/pages/admin-page.test.tsx`

- [ ] Write failing tests for image validation/path generation and signed-in admin controls.
- [ ] Implement Supabase email/password login and admin check.
- [ ] Implement recipe form, fixed tutorial fields, and WebP image upload.
- [ ] Implement cooking completion with photos, rating, and reflection.
- [ ] Run admin tests.

### Task 6: Visual design and deployment

**Files:**
- Replace: `src/index.css`
- Replace: `src/App.css`
- Modify: `index.html`
- Modify: `vite.config.ts`
- Create: `.github/workflows/deploy.yml`

- [ ] Apply warm journal styling, yellow Chen theme, pink Jin theme, responsive mobile layouts, and clear focus states.
- [ ] Set Vite base to `/chef-menu/`.
- [ ] Add GitHub Pages Actions workflow with Supabase repository secrets.
- [ ] Run tests, lint, and production build.
- [ ] Open the local production app in the browser and verify the main mobile and desktop flows.
