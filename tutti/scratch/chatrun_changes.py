    def _generate_plan(self, job, user, cfg, text):
        snap = self.snap_fn() or {}
        cands, _ = chat.candidates(cfg, snap, user, self.owner_fn(), self.own_machine_fn(user), pick='auto')
        if not cands:
            self._say(job, 'I need to plan this, but no model is available to write a plan.')
            return
        
        cand = cands[0]
        # System prompt for planning
        system = ("You are an expert architect. Analyze the user's request and write a plan in the following structured format:\n\n"
                  "## Goal\n[Goal description]\n\n"
                  "## Approach\n[Approach description]\n\n"
                  "## Risks\n[Risks description]\n\n"
                  "## Steps\n- [Step 1] [size: XS, S, M, L]\n- [Step 2] [size: XS, S, M, L]\n...")
        
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}]
        
        # Stream the planning response
        self._say(job, f'Planning your request with {cand["label"]}...')
        
        try:
            for piece in self.streams(cand, cfg, messages, job.cancel):
                if isinstance(piece, str):
                    job.emit('delta', text=piece)
                    job.msg['text'] += piece
        except Exception as e:
            self._say(job, f'Planning failed: {e}')
