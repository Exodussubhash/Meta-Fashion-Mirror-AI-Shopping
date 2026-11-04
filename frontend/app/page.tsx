'use client';

import { useEffect, useRef, useState } from 'react';
import PreviewFeedback, { type Feedback } from './components/PreviewFeedback';
import {
  ArrowUpRight,
  ArrowRight,
  Search,
  Sparkles,
  Upload,
  UserRound,
  Star,
  Check,
  ChevronLeft,
  ChevronRight,
  X,
  SlidersHorizontal,
  ImagePlus,
  LoaderCircle,
  ShieldCheck,
  RotateCcw,
  Trash2,
} from 'lucide-react';
type Product = {
  id: string;
  title: string;
  merchant: string;
  image: string;
  price: number;
  currency: string;
  url: string | null;
  colours: string[];
  sizes: string[];
};
type PhotoAssessment = {
  status: 'checking' | 'ready' | 'needs_new_photo' | 'unavailable';
  person_count?: number;
  face_visible?: boolean;
  visible_areas?: string[];
  reason?: string;
};
type Job = {
  id: string;
  product_id: string;
  status: string;
  image?: string;
  message?: string;
};
type Result = {
  products: Product[];
  pagination: {
    has_next_page?: boolean;
    cursor?: string;
    total_count?: number;
  };
  mode: string;
  query: string;
  reply?: string;
  warnings?: string[];
  filters?: Record<string, unknown> | null;
  context?: Record<string, unknown> | null;
};
const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const suggestions = ['Black oversized tees under £40', 'A relaxed weekend look', 'Neutral wardrobe essentials'];
export default function Home() {
  // Session, catalogue and presentation state.
  const [sid, setSid] = useState('');
  const [result, setResult] = useState<Result | null>(null);
  const [query, setQuery] = useState('');
  const [activeQuery, setActiveQuery] = useState('everyday fashion');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [photo, setPhoto] = useState('');
  const [uploading, setUploading] = useState(false);
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [saved, setSaved] = useState<string[]>([]);
  const [savedOnly, setSavedOnly] = useState(false);
  const [tab, setTab] = useState('Discover');
  const [messages, setMessages] = useState<{
    role: string;
    text: string;
  }[]>([]);
  const [chat, setChat] = useState('');
  const [chatBusy, setChatBusy] = useState(false);
  const [page, setPage] = useState(1);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [sort, setSort] = useState('recommended');
  const [details, setDetails] = useState<Product | null>(null);
  const [uploadMessage, setUploadMessage] = useState('');
  const [wearerDepartment, setWearerDepartment] = useState<'male' | 'female' | null>(null);
  const [photoAssessment, setPhotoAssessment] = useState<PhotoAssessment | null>(null);
  const [deletingPhoto, setDeletingPhoto] = useState(false);
  const previewDialog = useRef<HTMLDialogElement>(null);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [previewJobId, setPreviewJobId] = useState<string | null>(null);
  const [previewFeedback, setPreviewFeedback] = useState<Record<string, Feedback>>({});
  async function saveFeedback(jobId: string, feedback: Feedback) {
    await request('/api/try-on/' + jobId + '/feedback', {
      method: 'POST',
      body: JSON.stringify(feedback)
    });
    setPreviewFeedback(current => ({
      ...current,
      [jobId]: feedback
    }));
  }
  // Version counters prevent stale searches and polls from replacing newer results.
  const file = useRef<HTMLInputElement>(null);
  const epoch = useRef(0);
  const previewVersion = useRef(0);
  const retryingProducts = useRef(new Set<string>());
  // Attach the private session token to backend requests; uploads set their own content type.
  async function request(path: string, options: RequestInit = {}, token = sid) {
    const res = await fetch(API + path, {
      ...options,
      headers: {
        ...(options.body instanceof FormData ? {} : {
          'Content-Type': 'application/json'
        }),
        'X-Session-ID': token,
        ...options.headers
      }
    });
    const data = await res.json();
    if(!res.ok) throw Object.assign(Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong. Please retry.'), {
      status: res.status
    });
    return data;
  }
  const department: 'male' | 'female' | null = wearerDepartment || (tab === 'Men' ? 'male' : tab === 'Women' ? 'female' : null);
  async function search(q: string, cursor: string | null = null, token = sid, newPage = 1, selectedDepartment = department, preserveFilters = false) {
    const id = ++epoch.current;
    setBusy(true);
    setError('');
    try {
      const data = await request('/api/search', {
        method: 'POST',
        body: JSON.stringify({
          query: q,
          cursor,
          department: selectedDepartment,
          ...(preserveFilters ? {
            filters: result?.filters,
            context: result?.context
          } : {})
        })
      }, token);
      if(id !== epoch.current) return;
      setResult(data);
      setActiveQuery(q);
      setPage(newPage);
      setJobs({});
      if(newPage === 1) setCursors([null]);
    } catch(e) {
      if(id === epoch.current) setError((e as Error).message);
    } finally {
      if(id === epoch.current) setBusy(false);
    }
  }
  // Restore the existing browser session and photo before the initial catalogue search.
  useEffect(() => {
    let live = true;
    async function init() {
      try {
        let token = sessionStorage.getItem('fashionmirror-session');
        let state: {
          has_photo: boolean;
          wearer_department: 'male' | 'female' | null;
          photo_assessment: PhotoAssessment | null;
        } | null = null;
        if(token) {
          const response = await fetch(API + '/api/session', {
            headers: {
              'X-Session-ID': token
            }
          });
          if(response.status === 401) token = null; else if(!response.ok) throw Error('Session check failed'); else state = await response.json();
        }
        if(!live) return;
        if(!token) {
          const response = await fetch(API + '/api/session', {
            method: 'POST'
          });
          if(!response.ok) throw Error('Session creation failed');
          token = (await response.json()).session_id as string;
          if(!live) return;
          sessionStorage.setItem('fashionmirror-session', token);
          sessionStorage.removeItem('fashionmirror-photo');
        }
        if(!live) return;
        const selected = state?.wearer_department || null;
        setSid(token);
        setWearerDepartment(selected);
        setPhotoAssessment(state?.photo_assessment || null);
        setPhoto(state?.has_photo ? sessionStorage.getItem('fashionmirror-photo') || '' : '');
        await search('everyday fashion', null, token, 1, selected);
      } catch {
        if(live) setError('Cannot reach the fashion catalogue. Start the backend and try again.');
      }
    }
    init();
    return () => {
      live = false;
    };
  }, []);
  // Submit only the displayed page; backend job reuse prevents duplicate generations.
  useEffect(() => {
    const version = ++previewVersion.current;
    if(!sid || !photo || uploading || deletingPhoto || photoAssessment?.status !== 'ready' || !wearerDepartment || !result?.products.length) return;
    let stopped = false;
    request('/api/try-on/batch', {
      method: 'POST',
      body: JSON.stringify({
        product_ids: result.products.map(p => p.id)
      })
    }).then(data => {
      if(!stopped && previewVersion.current === version) {
        setJobs(Object.fromEntries(data.jobs.map((j: Job) => [j.product_id, j])));
      }
    }).catch(e => {
      if(!stopped) setError(e.message);
    });
    return () => {
      stopped = true;
      previewVersion.current++;
    };
  }, [sid, photo, result, photoAssessment, wearerDepartment, uploading, deletingPhoto]);

  // Poll the current jobs, including a single product submitted through Retry.
  useEffect(() => {
    const pending = Object.values(jobs).filter(j => ['queued', 'processing'].includes(j.status));
    if(!sid || !pending.length) return;
    let stopped = false,
      inFlight = false;
    const timer = setInterval(async () => {
      if(inFlight) return;
      inFlight = true;
      try {
        const updates: Job[] = await Promise.all(pending.map(j => request('/api/try-on/' + j.id)));
        if(!stopped) setJobs(previous => {
          const next = {
            ...previous
          };
          for(const job of updates) {
            // An older poll must never overwrite a freshly retried job.
            if(next[job.product_id]?.id === job.id) next[job.product_id] = job;
          }
          return next;
        });
      } catch(e) {
        if(!stopped) {
          const status = (e as Error & {
            status?: number;
          }).status;
          if(status === 401 || status === 404) {
            stopped = true;
            clearInterval(timer);
            setJobs({});
            setError(status === 401 ? 'Your session expired. Refresh to reconnect.' : 'These preview jobs are no longer available.');
          } else setError('Could not refresh previews. Checking again shortly.');
        }
      } finally {
        inFlight = false;
      }
    }, 1500);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [sid, jobs]);

  // Retry one failed product without resubmitting the rest of the page.
  async function retryPreview(productId: string) {
    const previous = jobs[productId];
    if(!sid || !photo || uploading || deletingPhoto || photoAssessment?.status !== 'ready' || previous?.status !== 'failed' || retryingProducts.current.has(productId)) return;
    const version = previewVersion.current;
    retryingProducts.current.add(productId);
    setJobs(current => ({
      ...current,
      [productId]: {
        ...previous,
        status: 'retrying',
        message: undefined
      }
    }));
    try {
      const data = await request('/api/try-on/batch', {
        method: 'POST',
        body: JSON.stringify({
          product_ids: [productId],
          retry_failed: true
        })
      });
      const job: Job | undefined = data.jobs.find((j: Job) => j.product_id === productId);
      if(!job) throw Error('Could not start this preview. Please retry.');
      if(previewVersion.current === version) setJobs(current => ({
        ...current,
        [productId]: job
      }));
    } catch(e) {
      if(previewVersion.current === version) setJobs(current => ({
        ...current,
        [productId]: {
          ...previous,
          status: 'failed',
          message: (e as Error).message
        }
      }));
    } finally {
      retryingProducts.current.delete(productId);
    }
  }
  // Keep the local thumbnail while the backend owns the normalized photo and assessment.
  async function upload(f: File) {
    if(!wearerDepartment) {
      setError('Choose Men’s clothing or Women’s clothing for this photo first.');
      return;
    }
    if(f.size > 10 * 1024 * 1024) {
      setError('Choose a photo smaller than 10 MB.');
      return;
    }
    const selected = wearerDepartment;
    setUploading(true);
    setError('');
    setUploadMessage('');
    setJobs({});
    setPhotoAssessment({
      status: 'checking'
    });
    try {
      const body = new FormData();
      body.append('file', f);
      body.append('department', selected);
      const uploaded = await request('/api/upload', {
        method: 'POST',
        body
      });
      setUploadMessage(uploaded.message || 'Photo saved successfully.');
      setPhotoAssessment(uploaded.photo_assessment);
      setWearerDepartment(uploaded.wearer_department);
      const image = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = reject;
        reader.readAsDataURL(f);
      });
      setPhoto(image);
      try {
        sessionStorage.removeItem('fashionmirror-photo');
        sessionStorage.setItem('fashionmirror-photo', image);
      } catch { }
      await search(activeQuery, null, sid, 1, selected);
    } catch(e) {
      setPhotoAssessment({
        status: 'unavailable',
        reason: (e as Error).message
      });
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }
  async function removePhoto() {
    if(deletingPhoto || uploading) return;
    setDeletingPhoto(true);
    setError('');
    try {
      await request('/api/upload', {
        method: 'DELETE'
      });
      previewVersion.current++;
      setPhoto('');
      setPhotoAssessment(null);
      setWearerDepartment(null);
      setUploadMessage('');
      setJobs({});
      setPreviewImage(null);
      previewDialog.current?.close();
      sessionStorage.removeItem('fashionmirror-photo');
    } catch(e) {
      setError((e as Error).message);
    } finally {
      setDeletingPhoto(false);
    }
  }
  // Chat returns the updated grid plus the model-created filters for later pagination.
  async function send(text: string) {
    if(!text.trim() || chatBusy) return;
    setChat('');
    setChatBusy(true);
    setMessages(m => [...m, {
      role: 'user',
      text
    }]);
    setError('');
    const id = ++epoch.current;
    try {
      const data = await request('/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          message: text,
          department
        })
      });
      if(id === epoch.current) {
        setResult(data);
        setActiveQuery(data.query);
        setPage(1);
        setCursors([null]);
        setJobs({});
      }
      setMessages(m => [...m, {
        role: 'assistant',
        text: data.reply
      }]);
    } catch(e) {
      setMessages(m => [...m, {
        role: 'assistant',
        text: (e as Error).message
      }]);
    } finally {
      setChatBusy(false);
    }
  }
  const products = [...(result?.products || [])].filter(p => !savedOnly || saved.includes(p.id)).sort((a, b) => sort === 'low' ? a.price - b.price : sort === 'high' ? b.price - a.price : 0);
  // A saved photo keeps its declared clothing department until it is removed.
  function category(name: string) {
    const requested = name === 'Men' ? 'male' : name === 'Women' ? 'female' : null;
    if(photo && requested && requested !== wearerDepartment) {
      setError('This photo is set to ' + (wearerDepartment === 'male' ? 'Men’s' : 'Women’s') + ' clothing. Remove it before changing the wearer’s department.');
      return;
    }
    const selectedDepartment = photo ? wearerDepartment : requested || wearerDepartment;
    if(!photo && requested) setWearerDepartment(requested);
    const nextQuery = name === 'Accessories' ? 'accessories' : 'everyday fashion';
    previewVersion.current++;
    setTab(name);
    setSavedOnly(false);
    setQuery('');
    setResult(null);
    setJobs({});
    search(nextQuery, null, sid, 1, selectedDepartment);
  }
  return <>
    <div className="announcement">A wardrobe that feels like you. <span>Meet your new way to shop <ArrowUpRight size={12} />
    </span>
    </div>
    <header>
      <a className="logo" href="/">fashion<span>mirror</span>
        <span className="logo-dot">✳</span>
      </a>
      <nav>
        {['Discover', 'Women', 'Men', 'Accessories'].map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => category(t)}>
          {t}
        </button>)}
      </nav>
      <div className="header-actions">
        <button
          className={savedOnly ? 'saved-active' : ''}
          onClick={() => setSavedOnly(!savedOnly)}
          aria-label="Show saved products"
        >
          <Star size={20} />
          {saved.length > 0 && <sup>
            {saved.length}
          </sup>}
        </button>
        <button onClick={() => file.current?.click()} aria-label="Upload your photo">
          <UserRound size={20} />
        </button>
      </div>
    </header>

    <main>
      <div className="breadcrumb">Home <span>/</span> Discover your style</div>
      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">
            <span /> YOUR STYLE. REFLECTED.</div>
          <h1>Find your next<br />“that’s so <em>me.</em>”</h1>
          <p>Great finds from independent brands.<br />A personal stylist that gets you. All in one mirror.</p>
          <button
            className="dark-button"
            onClick={() => document.getElementById('catalogue')?.scrollIntoView({
              behavior: 'smooth'
            })}
          >Discover your edit <ArrowRight size={16} />
          </button>
          <div className="hero-proof">
            <span>✳</span> A little AI. A lot of you.</div>
        </div>
        <div className="hero-photos">
          <div className="photo-one">
            <img
              alt="Neutral everyday fashion inspiration"
              src="https://images.unsplash.com/photo-1483985988355-763728e1935b?auto=format&fit=crop&w=850&q=85"
            />
          </div>
          <div className="photo-two">
            <img
              alt="Thoughtful textures and personal style"
              src="https://images.unsplash.com/photo-1485230895905-ec40ba36b9bc?auto=format&fit=crop&w=650&q=85"
            />
          </div>
          <div className="floating-label">
            <Sparkles size={17} />
            <div>Made for your kind of style.<small>Discover it. Make it yours.</small>
            </div>
          </div>
          <span className="hero-star">✳</span>
        </div>
      </section>

      <div className="benefits">
        <span>
          <Sparkles size={16} /> Your own AI stylist</span>
        <i />
        <span>
          <ImagePlus size={16} /> See your style, side by side</span>
        <i />
        <span>
          <ShieldCheck size={16} /> Discover independent brands</span>
        <i />
        <span>Powered by Shopify <span className="shopify-mark">▰</span>
        </span>
      </div>

      {/* Photo controls and conversational search beside the product catalogue. */}

      <section className="shop-layout" id="catalogue">
        <aside>
          <div className="photo-panel">
            <div className="panel-top">
              <span className="mini-icon">
                <UserRound size={17} />
              </span>
              <span>YOUR MIRROR</span>
              <span className="small-star">✳</span>
            </div>
            <h3>Make it personal.</h3>
            <p>One photo with your face and body.<br />A more personal way to discover.</p>
            <label className="wearer-choice">Clothing department for this photo<select
              aria-label="Clothing department for this photo"
              value={wearerDepartment || ''}
              disabled={!!photo || uploading || deletingPhoto}
              onChange={e => setWearerDepartment(e.target.value as 'male' | 'female' || null)}
            >
              <option value="" disabled>Choose a department</option>
              <option value="male">Men’s clothing</option>
              <option value="female">Women’s clothing</option>
            </select>
            </label>
            <small className="wearer-help">For you or a friend. This choice stays with the photo; All finds uses this department. Delete the photo to change it.</small>
            <button
              className={'upload-box ' + (photo ? 'has-photo' : '')}
              onClick={() => file.current?.click()}
              disabled={!sid || !wearerDepartment || uploading || deletingPhoto}
            >
              {photo ? <>
                <img src={photo} alt="Your uploaded photograph" />
                <span>
                  <Check size={14} /> Your photo is ready</span>
              </> : <>
                <span className="upload-circle">
                  {uploading ? <LoaderCircle className="spin" size={22} /> : <Upload size={22} />}
                </span>
                <strong>
                  {uploading ? 'Uploading & checking…' : 'Add your photo'}
                </strong>
                <small>JPG, PNG or WebP · up to 10 MB</small>
              </>}
            </button>
            <input
              ref={file}
              type="file"
              hidden
              disabled={!sid || !wearerDepartment || uploading || deletingPhoto}
              accept="image/jpeg,image/png,image/webp"
              onChange={e => {
                if(e.target.files?.[0]) upload(e.target.files[0]);
                e.target.value = '';
              }}
            />
            {photo ? <button
              type="button"
              className="photo-delete"
              onClick={removePhoto}
              disabled={deletingPhoto || uploading || !sid}
              aria-label="Delete your uploaded photo"
              aria-busy={deletingPhoto}
            >
              {deletingPhoto ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />} {deletingPhoto ? 'Deleting photo…' : 'Delete photo'}
            </button> : <div className="photo-tip">
              <ShieldCheck size={12} /> Private to your session. Always yours.</div>}
            {photoAssessment && <div role="status" className="assessment-status">
              <strong>
                {photoAssessment.status === 'checking' ? 'Checking your photo…' : photoAssessment.status === 'ready' ? 'Photo check complete' : 'Previews paused'}
              </strong>
              <p>
                {photoAssessment.status === 'ready' ? `Visible areas: ${(photoAssessment.visible_areas || []).map(a => a.replace('_', ' ')).join(', ')}. Only compatible garments will generate.` : photoAssessment.reason || 'Muse Spark checks visible body areas before any preview starts.'}
              </p>
            </div>}
            <div className="preview-note">Your photo and each garment image are sent to the configured hosted image service for automatic previews. Muse Spark checks photo suitability once per unique photo and handles search. Generated previews are an approximation of fit.</div>
          </div>

          <div className="stylist-panel">
            <div className="stylist-title">
              <span className="mini-icon">
                <Sparkles size={18} />
              </span>
              <div>
                <strong>Your style sidekick</strong>
                <small>
                  <span /> Muse Spark 1.3</small>
              </div>
            </div>
            <div className="chat-body">
              <div className="assistant-bubble">Hi, I’m your AI stylist. Tell me what you’re looking for — a piece, a mood, or a whole new look. <span>✧</span>
              </div>
              <div className="suggestions">
                {suggestions.map(s => <button key={s} disabled={!sid || chatBusy} onClick={() => send(s)}>
                  {s}
                  <ArrowUpRight size={13} />
                </button>)}
              </div>
              {messages.map((m, i) => <div key={i} className={m.role === 'user' ? 'user-bubble' : 'assistant-bubble'}>
                {m.text}
              </div>)}
              {chatBusy && <div className="thinking">
                <LoaderCircle className="spin" size={14} /> Finding your style…</div>}
            </div>
            <form
              className="chat-input"
              onSubmit={e => {
                e.preventDefault();
                send(chat);
              }}
            >
              <input
                aria-label="Message your stylist"
                placeholder="Ask your stylist…"
                value={chat}
                onChange={e => setChat(e.target.value)}
              />
              <button aria-label="Send message" disabled={!sid || chatBusy || !chat.trim()}>
                <ArrowRight size={17} />
              </button>
            </form>
            <div className="chat-foot">A little inspiration goes a long way.</div>
          </div>
        </aside>

        <div className="catalogue">
          <div className="catalogue-heading">
            <div className="eyebrow">THE DISCOVERY EDIT</div>
            <h2>
              {savedOnly ? 'Your saved finds' : 'Good style. Great finds.'}
            </h2>
            <p>A new favourite is waiting to meet you.</p>
          </div>
          <form
            className="search-bar"
            onSubmit={e => {
              e.preventDefault();
              search(query || 'everyday fashion');
            }}
          >
            <Search size={19} />
            <input
              aria-label="Search clothing"
              placeholder="Try “black oversized T-shirts under £40”"
              value={query}
              onChange={e => setQuery(e.target.value)}
            />
            <button disabled={!sid || busy}>
              {busy ? <LoaderCircle size={17} className="spin" /> : <>Find my style <ArrowRight size={15} />
              </>}
            </button>
          </form>
          <div className="category-pills">
            {['All finds', 'T-shirts', 'Knitwear', 'Shirts', 'Jeans', 'Outerwear'].map(c => <button
              key={c}
              className={c === 'All finds' && activeQuery === 'everyday fashion' || activeQuery.toLowerCase() === c.toLowerCase() ? 'selected' : ''}
              onClick={() => {
                setQuery(c === 'All finds' ? '' : c);
                search(c === 'All finds' ? 'everyday fashion' : c);
              }}
            >
              {c}
            </button>)}
          </div>
          <div className="results-toolbar">
            <span>
              {busy ? 'Finding your next favourites…' : `${products.length} finds${result?.pagination.total_count ? ` of ${result.pagination.total_count}` : ''}`} <span className="muted">· {savedOnly ? 'saved on this page' : 'curated for discovery'}
              </span>
            </span>
            <label>
              <SlidersHorizontal size={14} />
              <select aria-label="Sort products" value={sort} onChange={e => setSort(e.target.value)}>
                <option value="recommended">Recommended</option>
                <option value="low">Price: low to high</option>
                <option value="high">Price: high to low</option>
              </select>
            </label>
          </div>
          {result?.mode === 'mock' && <div className="mode-banner">DEVELOPMENT MODE <span>Sample products · no live inventory or AI-generated previews</span>
          </div>}
          {result?.warnings?.map((w, i) => <div role="status" className="mode-banner" key={i}>
            {w}
          </div>)}
          {error && <div role="alert" className="error">
            {error}
            <button onClick={() => sid ? search(activeQuery) : window.location.reload()}>Retry <ArrowRight size={14} />
            </button>
          </div>}

          {photo && <div role="status" className="upload-status">
            <Check size={18} />
            <div>
              <strong>
                {photoAssessment?.status === 'ready' ? 'Photo checked — compatible previews enabled' : 'Photo saved — previews paused'}
              </strong>
              <p>
                {uploadMessage || 'Only products matching this photo’s clothing department, visible body areas and confirmed category can generate previews.'}
              </p>
            </div>
          </div>}

          {/* Each product pairs original merchant photography with its own preview status. */}

          <div className="product-grid" aria-busy={busy}>
            {busy ? Array.from({
              length: 6
            }, (_, i) => <div className="skeleton" key={i} />) : products.map((p, i) => {
              const job = jobs[p.id];
              const skipped = job?.status === 'blocked' || !!photo && photoAssessment?.status !== 'ready';
              return <article className="product-card" key={p.id}>
                <div className="product-visual">
                  <div className="original">
                    <img src={p.image || undefined} alt={p.title} loading="lazy" />
                    <span className="image-label">THE ORIGINAL</span>
                  </div>
                  <div className="personal-preview">
                    {job?.status === 'completed' && job.image ? <button
                      type="button"
                      className="generated-preview-button"
                      aria-label={`Enlarge your preview of ${p.title}`}
                      onClick={() => {
                        setPreviewImage(job.image!);
                        setPreviewJobId(job.id);
                        previewDialog.current?.showModal();
                      }}
                    >

                      <img src={job.image} alt={`Your personalized preview of ${p.title}`} />

                    </button> : <div className="preview-content">
                      {job && ['queued', 'processing', 'retrying'].includes(job.status) ? <>
                        <LoaderCircle size={24} className="spin" />
                        <strong>
                          {job.status === 'retrying' ? 'Retrying preview' : job.status === 'queued' ? 'In the queue' : 'Creating your look'}
                        </strong>
                        <small>Made just for you</small>
                      </> : <>
                        <span className="mirror-outline">
                          <UserRound size={27} />
                        </span>
                        <strong>
                          {skipped ? 'Preview skipped' : job?.status === 'failed' ? 'Preview failed' : 'Preview unavailable'}
                        </strong>
                        <small>
                          {skipped ? job?.message || photoAssessment?.reason || 'Photo suitability check required.' : photo ? job?.status === 'failed' ? 'Please try again later' : job?.message || 'Checking product compatibility…' : 'Choose a department and add a photo'}
                        </small>
                        {job?.status === 'failed' && <>
                          <small>
                            {job.message}
                          </small>
                          <button
                            type="button"
                            className="retry-preview"
                            aria-label={'Retry preview for ' + p.title}
                            disabled={!sid || !photo || uploading || deletingPhoto || busy}
                            onClick={() => retryPreview(p.id)}
                          >
                            <RotateCcw size={12} /> Retry preview</button>
                        </>}
                      </>}
                    </div>}
                    {job?.status === 'completed' && job.image && <PreviewFeedback
                      key={job.id}
                      saved={previewFeedback[job.id]}
                      onSave={feedback => saveFeedback(job.id, feedback)}
                    />}
                    <span className="image-label">
                      <Sparkles size={9} /> ON YOU</span>
                  </div>
                  <button
                    className={'save-button ' + (saved.includes(p.id) ? 'is-saved' : '')}
                    aria-label={saved.includes(p.id) ? 'Unsave ' + p.title : 'Save ' + p.title}
                    onClick={() => setSaved(s => s.includes(p.id) ? s.filter(id => id !== p.id) : [...s, p.id])}
                  >
                    <Star size={15} fill={saved.includes(p.id) ? 'currentColor' : 'none'} />
                  </button>
                  {i === 0 && <div className="card-tag">THE EVERYDAY EDIT</div>}
                </div>
                <div className="product-info">
                  <div className="merchant">
                    {p.merchant}
                  </div>
                  <button className="product-name" onClick={() => setDetails(p)}>
                    {p.title}
                  </button>
                  <div className="price-row">
                    <strong>
                      {new Intl.NumberFormat('en-GB', {
                        style: 'currency',
                        currency: p.currency || 'GBP'
                      }).format(p.price)}
                    </strong>
                    <div className="swatches">
                      {p.colours.slice(0, 4).map(c => <span
                        title={c}
                        key={c}
                        style={{
                          background: ({
                            cream: '#ddd4c2',
                            black: '#292929',
                            white: '#fafafa',
                            blue: '#667c99',
                            brown: '#9c7860'
                          } as Record<string, string>)[c.toLowerCase()] || '#b9aea1'
                        }}
                      />)}
                      {p.colours.length > 4 && <small>+{p.colours.length - 4}
                      </small>}
                    </div>
                  </div>
                  <div className="size-line">
                    {p.sizes.length ? p.sizes.join(' · ') : 'Sizes available at merchant'}
                    <button aria-label={'Details for ' + p.title} onClick={() => setDetails(p)}>
                      <ArrowUpRight size={15} />
                    </button>
                  </div>
                </div>
              </article>;
            })}
          </div>
          {!busy && !products.length && result && !error && <div className="empty">
            <Search size={30} />
            <h3>
              {savedOnly ? 'Your favourites start here.' : 'No finds just yet.'}
            </h3>
            <p>
              {savedOnly ? 'Tap a star to save a piece you love.' : 'Try a different colour, style or price range.'}
            </p>
          </div>}

          <div className="pagination">
            <span>Little edits. Endless possibilities.</span>
            <div>
              <button
                aria-label="Previous page"
                disabled={page === 1 || busy}
                onClick={() => search(activeQuery, cursors[page - 2], sid, page - 1, department, true)}
              >
                <ChevronLeft size={16} />
              </button>
              <span>Page {page}
              </span>
              <button
                aria-label="Next page"
                disabled={!result?.pagination.has_next_page || busy}
                onClick={() => {
                  const c = result?.pagination.cursor || null;
                  setCursors(prev => [...prev.slice(0, page), c]);
                  search(activeQuery, c, sid, page + 1, department, true);
                }}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </div>
        </div>
      </section>
      <section className="bottom-strip">
        <span className="large-star">✳</span>
        <div>
          <h3>Your style is personal. Shopping should be, too.</h3>
          <p>Discover something that feels a little more you.</p>
        </div>
        <button onClick={() => file.current?.click()}>Meet your mirror <ArrowUpRight size={17} />
        </button>
      </section>
    </main>
    <footer>
      <a className="logo" href="/">fashion<span>mirror</span>
        <span className="logo-dot">✳</span>
      </a>
      <span>Independent finds. Individual style.</span>
      <small>© {new Date().getFullYear()} FashionMirror AI</small>
    </footer>

    <dialog
      ref={previewDialog}
      className="image-preview-dialog"
      aria-label="Generated outfit preview"
      onClose={() => {
        setPreviewImage(null);
        setPreviewJobId(null);
      }}
      onClick={event => {
        if(event.target === event.currentTarget) {
          previewDialog.current?.close();
        }
      }}
    >

      <button
        type="button"
        className="modal-close"
        aria-label="Close image preview"
        autoFocus
        onClick={() => previewDialog.current?.close()}
      >

        <X />

      </button>

      {previewImage && <div className="zoom-preview-image">

        <img src={previewImage} alt="Your generated outfit preview" />

        {previewJobId && <PreviewFeedback
          key={previewJobId}
          saved={previewFeedback[previewJobId]}
          onSave={feedback => saveFeedback(previewJobId, feedback)}
        />}

      </div>}

    </dialog>
    {details && <div className="modal-backdrop" onClick={() => setDetails(null)}>
      <section
        role="dialog"
        aria-modal="true"
        aria-label="Product details"
        className="product-modal"
        onClick={e => e.stopPropagation()}
      >
        <button className="modal-close" aria-label="Close details" onClick={() => setDetails(null)}>
          <X />
        </button>
        <img src={details.image} alt={details.title} />
        <div>
          <div className="eyebrow">
            {details.merchant}
          </div>
          <h2>
            {details.title}
          </h2>
          <p>
            {new Intl.NumberFormat('en-GB', {
              style: 'currency',
              currency: details.currency
            }).format(details.price)}
          </p>
          <p>Colours: {details.colours.join(', ') || 'See merchant'}
          </p>
          <p>Sizes: {details.sizes.join(', ') || 'See merchant'}
          </p>
          {details.url && /^https:\/\//.test(details.url) ? <a className="dark-button" href={details.url} target="_blank" rel="noopener noreferrer">Shop at merchant <ArrowUpRight size={16} />
          </a> : <p className="muted">
            {result?.mode === 'mock' ? 'Sample product — shopping links are disabled.' : 'Merchant link unavailable.'}
          </p>}
        </div>
      </section>
    </div>}
  </>;
}
