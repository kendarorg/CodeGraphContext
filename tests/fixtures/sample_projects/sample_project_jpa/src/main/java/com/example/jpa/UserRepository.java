package com.example.jpa;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

public interface UserRepository extends JpaRepository<User, Long> {
    @Query("SELECT u FROM users u WHERE u.name = :name")
    List<User> matchUsersByName(String name);

    @Query("UPDATE users SET name = :name WHERE id = :id")
    int rename(Long id, String name);
}
