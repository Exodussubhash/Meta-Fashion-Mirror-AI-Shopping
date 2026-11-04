'use client';

import { useState } from 'react';
import { Heart, ThumbsDown, MessageCircle } from 'lucide-react';
export type Feedback = {
  rating: 'like' | 'dislike';
  reason?: string;
};
const reasons = [['matches_expectations', 'Matches my expectations'], ['wrong_garment_or_colour', 'Wrong garment or colour'], ['identity_changed', 'Face / identity changed'], ['anatomy_incorrect', 'Body / anatomy incorrect'], ['unrealistic_fit', 'Poor fit or unrealistic result']];
export default function PreviewFeedback({
  saved,
  onSave
}: {
  saved?: Feedback;
  onSave: (feedback: Feedback) => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // Parent state keeps the card and zoom popup in sync after the CSV write succeeds.
  async function submit(feedback: Feedback) {
    if (saving) return;
    if (saved?.rating === feedback.rating && saved?.reason === feedback.reason) {
      setOpen(false);
      return;
    }
    setSaving(true);
    setError('');
    try {
      await onSave(feedback);
      setOpen(false);
    } catch {
      setError('Could not save. Please retry.');
    } finally {
      setSaving(false);
    }
  }
  return <div className="preview-feedback">


    <div className="feedback-actions" aria-label="Rate this preview">


      <button
        type="button"
        title="Like preview"
        aria-label="Like preview"
        aria-pressed={saved?.rating === 'like'}
        disabled={saving}
        onClick={() => submit({
          rating: 'like'
        })}
      >


        <Heart size={16} fill={saved?.rating === 'like' ? 'currentColor' : 'none'} />


      </button>


      <button
        type="button"
        title="Dislike preview"
        aria-label="Dislike preview"
        aria-pressed={saved?.rating === 'dislike'}
        disabled={saving}
        onClick={() => submit({
          rating: 'dislike'
        })}
      >


        <ThumbsDown size={16} fill={saved?.rating === 'dislike' ? 'currentColor' : 'none'} />


      </button>


      <button
        type="button"
        title="Tell us why"
        aria-label="Tell us why"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <MessageCircle size={16} />
      </button>


    </div>


    {open && <div className="feedback-options">


      <strong>How was this preview?</strong>


      {reasons.map(([reason, label]) => <button
        type="button"
        key={reason}
        disabled={saving}
        aria-pressed={saved?.reason === reason}
        onClick={() => submit({
          rating: reason === 'matches_expectations' ? 'like' : 'dislike',
          reason
        })}
      >
        {label}
      </button>)}


    </div>}


    <span className="feedback-status" role="status">
      {error || (saving ? 'Saving…' : saved ? 'Feedback saved' : '')}
    </span>


  </div>;
}
