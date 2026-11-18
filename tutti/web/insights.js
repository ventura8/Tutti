
function renderInsights(s) {
    const insights = s.profile_insights;
    if (!insights) return;
    const container = $('insights');
    const content = [
        el('h3', { class: 'card__sub', text: 'Owner Profile' }),
        el('p', { text: `Start Date: ${insights.owner.start_date}` }),
        el('p', { text: `Core Value: ${insights.owner.core_value}` }),
        el('h3', { class: 'card__sub', text: 'Presence Patterns' })
    ];
    for (const [name, data] of Object.entries(insights.presence)) {
        content.push(el('p', { text: `${name}: ${data.label} (Always-on: ${data.always_on})` }));
    }
    content.push(el('h3', { class: 'card__sub', text: 'Household Use' }));
    content.push(el('p', { text: insights.usage.headline }));
    
    container.replaceChildren(...content);
}
