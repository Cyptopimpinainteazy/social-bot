// Tip Received handler. Room-level acknowledgement deliberately names no tipper.
const message = $settings.tipThanksText;
if (typeof message === "string" && message.trim()) {
    const seconds = Number($settings.tipCooldownSeconds);
    const wait = Number.isFinite(seconds) && seconds > 0 ? Math.max(1, seconds) : 30;
    const last = $kv.get("socialbot_tip_notice_at");
    const now = Date.now();
    if (typeof last === "number" && (last === 0 || now - last >= wait * 1000)) {
        $kv.set("socialbot_tip_notice_at", now);
        $room.sendNotice(message);
    }
}
