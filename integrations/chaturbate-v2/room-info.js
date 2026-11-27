// Paste this into the Code handler for the V2 Shortcut named room-info.
// Edit the notice before testing it in your broadcaster's test room.
if ($shortcut.name === "room-info" && typeof $user.username === "string" && $user.username.trim()) {
    $room.sendNotice(
        "Welcome! You can find the stream schedule and official links in the broadcaster's bio.",
        { toUsername: $user.username }
    );
}
