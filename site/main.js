// Small progressive enhancements: sticky-nav border, scroll reveal and copy buttons.
(() => {
  'use strict';
  const root = document.documentElement;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if ('IntersectionObserver' in window && !reduce) {
    root.classList.add('js');
    const io = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    }, {rootMargin: '0px 0px -8% 0px', threshold: 0.12});
    document.querySelectorAll('.reveal').forEach(el => io.observe(el));
  }

  const nav = document.querySelector('.nav');
  const onScroll = () => nav.classList.toggle('scrolled', scrollY > 8);
  addEventListener('scroll', onScroll, {passive: true});
  onScroll();

  document.querySelectorAll('.copy').forEach(btn => {
    btn.addEventListener('click', async () => {
      const text = btn.parentElement.querySelector('code').textContent;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = '已复制';
      } catch {
        btn.textContent = '请手动复制';
      }
      setTimeout(() => { btn.textContent = '复制'; }, 1600);
    });
  });
})();
