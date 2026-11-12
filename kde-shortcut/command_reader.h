#pragma once

#include <QByteArray>
#include <QSocketNotifier>

#include <cerrno>
#include <functional>
#include <unistd.h>

// Read only the bytes reported ready by the pipe. QTextStream::atEnd() and
// readLine() may wait for more data on an open stdin pipe and stall Qt's event
// loop (including the global-shortcut activation that must deliver Escape).
class CommandReader {
public:
    CommandReader(int fd, std::function<void(const QByteArray &)> handle)
        : notifier(fd, QSocketNotifier::Read)
    {
        QObject::connect(&notifier, &QSocketNotifier::activated,
                         [this, fd, handle](QSocketDescriptor, QSocketNotifier::Type) {
            char bytes[4096];
            const auto count = ::read(fd, bytes, sizeof(bytes));
            if (count <= 0) {
                if (count == 0 || (errno != EINTR && errno != EAGAIN)) {
                    notifier.setEnabled(false);
                }
                return;
            }
            for (qsizetype i = 0; i < count; ++i) {
                if (bytes[i] == '\n') {
                    if (!discarding) {
                        handle(pending.trimmed());
                    }
                    pending.clear();
                    discarding = false;
                } else if (!discarding) {
                    if (pending.size() < 128) {
                        pending.append(bytes[i]);
                    } else {
                        pending.clear();
                        discarding = true;
                    }
                }
            }
        });
    }

private:
    QSocketNotifier notifier;
    QByteArray pending;
    bool discarding = false;
};
