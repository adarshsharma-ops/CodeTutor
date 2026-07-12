import random
play = "Y"
playagain = "Y"

while playagain.upper() == play:
    randnum = random.randint(1,100)
    while True:
        try:
            r = int(input("Guess: "))
            if r < 1 or r > 100:
                raise ValueError("Number must be between 1 and 100")
            break
        except ValueError:
            print("Please ensure that you enter a valid number in between 1 and 100")
    
    count = 1

    while r != randnum:
        try:

            if r > randnum:
                print("Too High, try again")

            elif r < randnum:
                print("Too low, try again")
            r = int(input("Guess: "))
            count += 1
            if r < 1 or r > 100:
                raise ValueError("Number must be between 1 and 100")
                break

        except ValueError:
            print("Please enter a valid number")

    print(f"You took, {count}, attemps to guess the correct number")
    playagain: str = input("Would you like to play again? Answer in Y or N: ")


