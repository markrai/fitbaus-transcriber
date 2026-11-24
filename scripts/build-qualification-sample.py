from pathlib import Path
import subprocess
import sys
import tempfile
import wave


PASSAGES = (
    "The morning began quietly. A gardener opened the gate and followed the path toward the old greenhouse. "
    "Several small plants needed water, and the wooden shelves were covered in dust. She moved the pots one by one "
    "until there was enough space for the new seedlings. Outside, birds called from the trees, and a light wind "
    "carried the scent of rain. Before leaving, she wrote a short note about the work that remained.",
    "At the library, a visitor asked for a book about the history of ships. The librarian walked to the next room "
    "and returned with three books. One described the construction of sailing vessels, another discussed trade routes, "
    "and the third contained drawings of harbors. The visitor chose the second book and sat beside a window. "
    "He read for a while, stopped to take notes, and then returned the other volumes to the desk.",
    "A group of neighbors decided to repair a bench in the park. They measured the wooden boards and marked the places "
    "where new screws would be needed. While one person collected the tools, another cleaned the surface and removed "
    "the loose paint. They worked carefully, checking the measurements before making each cut. By the afternoon the "
    "bench was ready, and they sat together to discuss plans for the community garden.",
    "The train passed through a valley surrounded by hills. From the carriage window, the passengers could see a river "
    "winding between fields. At the next station a family boarded with two large bags and a small basket. The conductor "
    "checked the tickets and helped them find their seats. Soon the train continued toward the coast, where a narrow "
    "street led from the station to a market filled with fruit, bread, and flowers.",
    "In the kitchen, a bowl of flour stood beside a jar of oats. The recipe called for warm water and a little patience. "
    "After mixing the ingredients, the cook covered the dough and set it aside. During the wait she washed the dishes, "
    "opened the windows, and read a few pages from a magazine. Later she divided the dough into equal pieces and placed "
    "them on a tray. The house gradually filled with the smell of fresh bread.",
    "A student was learning how to describe the movement of the planets. He drew a circle on a sheet of paper and "
    "placed several smaller circles around it. The teacher explained that the drawing was only a model, not an exact "
    "picture of distances. They compared the drawing with a photograph and discussed the difference between observation "
    "and explanation. At the end of the lesson, each student wrote one question to investigate the following week.",
    "The workshop contained a collection of familiar objects. A lamp stood beside a radio, and a clock hung above "
    "the door. On the table were a ruler, a pencil, and several pieces of paper. The owner wanted to organize everything "
    "so that each tool could be found easily. He sorted the objects into groups, cleaned the drawers, and labeled the "
    "boxes. When the work was finished, he made a cup of tea and looked around the room.",
    "Along the shore, the water rose and fell against the stones. Two walkers stopped near a sign describing the "
    "local wildlife. They listened for a moment and watched a flock of birds cross the sky. A little farther ahead "
    "they found a quiet place to rest. They opened their bags, shared an apple, and talked about the places they hoped "
    "to visit next. Then they followed the same path back toward the village.",
)


def main(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        speech = Path(directory) / "speech.wav"
        text = "\n\n".join(PASSAGES * 2)
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(speech), "--stdin"],
                       input=text.encode("utf-8"), check=True)
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(speech), "-t", "300", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(destination)], check=True)
    with wave.open(str(destination), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (
                1, 2, 16000, 4800000):
            raise RuntimeError("Invalid automatic qualification fixture")


if __name__ == "__main__":
    main(sys.argv[1])
