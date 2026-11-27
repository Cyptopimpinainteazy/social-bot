// User Enter handler. Configure the setting names in the Developer Portal.
const greeting = $settings.greetingText;
const username = $user.username;
if (typeof greeting === "string" && greeting.trim() &&
        typeof username === "string" && username.trim()) {
    const seconds = Number($settings.greetingCooldownSeconds);
    const wait = Number.isFinite(seconds) && seconds > 0 ? Math.max(1, seconds) : 15;
    const last = $kv.get("socialbot_entry_notice_at");
    const now = Date.now();
    if (typeof last === "number" && (last === 0 || now - last >= wait * 1000)) {
        const names = typeof $settings.regularUsernames === "string" ? $settings.regularUsernames : "";
        const known = names.split(",").some(name => name.trim().toLowerCase() === username.toLowerCase());
        const regular = $settings.regularGreeting;
        const notice = known && typeof regular === "string" && regular.trim() ? regular : greeting;
        $kv.set("socialbot_entry_notice_at", now);
        $room.sendNotice(notice, { toUsername: username });
    }
}
