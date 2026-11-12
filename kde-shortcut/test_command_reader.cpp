#include "command_reader.h"

#include <QCoreApplication>
#include <QTimer>
#include <QStringList>

#include <cstdio>

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    int pipefd[2];
    if (::pipe(pipefd) != 0) return 1;
    QStringList commands;
    bool responsive = false;
    bool finished = false;
    CommandReader reader(pipefd[0], [&](const QByteArray &command) {
        commands.append(QString::fromUtf8(command));
        if (command == "CANCEL_ON") {
            // Keep the writer open: the old QTextStream loop hangs here.
            QTimer::singleShot(0, [&] {
                responsive = true;
                ::write(pipefd[1], "CANCEL_", 7);
                QTimer::singleShot(10, [&] {
                    ::write(pipefd[1], "OFF\nUNKNOWN\n", 12);
                });
            });
        }
        if (command == "UNKNOWN") {
            finished = true;
            app.quit();
        }
    });
    ::write(pipefd[1], "CANCEL_ON\n", 10);
    QTimer::singleShot(1000, &app, &QCoreApplication::quit);
    app.exec();
    const bool ok = responsive && finished
        && commands == QStringList{"CANCEL_ON", "CANCEL_OFF", "UNKNOWN"};
    ::close(pipefd[1]);
    ::close(pipefd[0]);
    if (!ok) std::fprintf(stderr, "Open-pipe command/event-loop regression failed\n");
    return ok ? 0 : 1;
}
